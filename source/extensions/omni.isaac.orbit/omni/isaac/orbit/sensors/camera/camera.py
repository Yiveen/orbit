# Copyright (c) 2022-2023, The ORBIT Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause

from __future__ import annotations

import math
import numpy as np
import re
import torch
from tensordict import TensorDict
from typing import TYPE_CHECKING, Any, Sequence
from typing_extensions import Literal

import omni.kit.commands
import omni.usd
from omni.isaac.core.prims import XFormPrimView
from pxr import UsdGeom

import omni.isaac.orbit.sim as sim_utils
from omni.isaac.orbit.utils import to_camel_case
from omni.isaac.orbit.utils.array import convert_to_torch
from omni.isaac.orbit.utils.math import quat_from_matrix

from ..sensor_base import SensorBase
from .camera_data import CameraData
from .utils import convert_orientation_convention, create_rotation_matrix_from_view

if TYPE_CHECKING:
    from .camera_cfg import CameraCfg


class Camera(SensorBase):
    r"""The camera sensor for acquiring visual data.

    This class wraps over the `UsdGeom Camera`_ for providing a consistent API for acquiring visual data.
    It ensures that the camera follows the ROS convention for the coordinate system.

    Summarizing from the `replicator extension`_, the following sensor types are supported:

    - ``"rgb"``: A rendered color image.
    - ``"distance_to_camera"``: An image containing the distance to camera optical center.
    - ``"distance_to_image_plane"``: An image containing distances of 3D points from camera plane along camera's z-axis.
    - ``"normals"``: An image containing the local surface normal vectors at each pixel.
    - ``"motion_vectors"``: An image containing the motion vector data at each pixel.
    - ``"instance_segmentation"``: The instance segmentation data.
    - ``"semantic_segmentation"``: The semantic segmentation data.

    .. note::
        Currently the following sensor types are not supported in a "view" format:

        - ``"bounding_box_2d_tight"``: The tight 2D bounding box data (only contains non-occluded regions).
        - ``"bounding_box_2d_loose"``: The loose 2D bounding box data (contains occluded regions).
        - ``"bounding_box_3d"``: The 3D view space bounding box data.

        In case you need to work with these sensor types, we recommend using the single camera implementation
        from the :mod:`omni.isaac.orbit.compat.camera` module.

    .. _replicator extension: https://docs.omniverse.nvidia.com/prod_extensions/prod_extensions/ext_replicator/annotators_details.html#annotator-output
    .. _USDGeom Camera: https://graphics.pixar.com/usd/docs/api/class_usd_geom_camera.html

    """

    cfg: CameraCfg
    """The configuration parameters."""
    UNSUPPORTED_TYPES: set[str] = {"bounding_box_2d_tight", "bounding_box_2d_loose", "bounding_box_3d"}
    """The set of sensor types that are not supported by the camera class."""

    def __init__(self, cfg: CameraCfg):
        """Initializes the camera sensor.

        Args:
            cfg: The configuration parameters.

        Raises:
            RuntimeError: If no camera prim is found at the given path.
            ValueError: If the provided data types are not supported by the camera.
        """
        # check if sensor path is valid
        # note: currently we do not handle environment indices if there is a regex pattern in the leaf
        #   For example, if the prim path is "/World/Sensor_[1,2]".
        sensor_path = cfg.prim_path.split("/")[-1]
        sensor_path_is_regex = re.match(r"^[a-zA-Z0-9/_]+$", sensor_path) is None
        if sensor_path_is_regex:
            raise RuntimeError(
                f"Invalid prim path for the camera sensor: {self.cfg.prim_path}."
                "\n\tHint: Please ensure that the prim path does not contain any regex patterns in the leaf."
            )
        # perform check on supported data types
        self._check_supported_data_types(cfg)
        # initialize base class
        super().__init__(cfg)

        # spawn the asset
        if self.cfg.spawn is not None:
            # compute the rotation offset
            rot = torch.tensor(self.cfg.offset.rot, dtype=torch.float32).unsqueeze(0)
            rot_offset = convert_orientation_convention(rot, origin=self.cfg.offset.convention, target="opengl")
            rot_offset = rot_offset.squeeze(0).numpy()
            # spawn the asset
            self.cfg.spawn.func(
                self.cfg.prim_path, self.cfg.spawn, translation=self.cfg.offset.pos, orientation=rot_offset
            )
        # check that spawn was successful
        matching_prims = sim_utils.find_matching_prims(self.cfg.prim_path)
        if len(matching_prims) == 0:
            raise RuntimeError(f"Could not find prim with path {self.cfg.prim_path}.")

        # UsdGeom Camera prim for the sensor
        self._sensor_prims: list[UsdGeom.Camera] = list()
        # Create empty variables for storing output data
        self._data = CameraData()

    def __del__(self):
        """Unsubscribes from callbacks and detach from the replicator registry."""
        # unsubscribe callbacks
        super().__del__()
        # delete from replicator registry
        for _, annotators in self._rep_registry.items():
            for annotator, render_product_path in zip(annotators, self._render_product_paths):
                annotator.detach([render_product_path])
                annotator = None

    def __str__(self) -> str:
        """Returns: A string containing information about the instance."""
        # message for class
        return (
            f"Camera @ '{self.cfg.prim_path}': \n"
            f"\tdata types   : {self.data.output.sorted_keys} \n"
            f"\tupdate period (s): {self.cfg.update_period}\n"
            f"\tshape        : {self.image_shape}\n"
            f"\tnumber of sensors : {self._view.count}"
        )

    """
    Properties
    """

    @property
    def num_instances(self) -> int:
        return self._view.count

    @property
    def data(self) -> CameraData:
        # update sensors if needed
        self._update_outdated_buffers()
        # return the data
        return self._data

    @property
    def frame(self) -> torch.tensor:
        """Frame number when the measurement took place."""
        return self._frame

    @property
    def render_product_paths(self) -> list[str]:
        """The path of the render products for the cameras.

        This can be used via replicator interfaces to attach to writes or external annotator registry.
        """
        return self._render_product_paths

    @property
    def image_shape(self) -> tuple[int, int]:
        """A tuple containing (height, width) of the camera sensor."""
        return (self.cfg.height, self.cfg.width)

    """
    Configuration
    """

    def set_intrinsic_matrices(
        self, matrices: torch.Tensor, focal_length: float = 1.0, env_ids: Sequence[int] | None = None
    ):
        """Set parameters of the USD camera from its intrinsic matrix.

        The intrinsic matrix and focal length are used to set the following parameters to the USD camera:

        - ``focal_length``: The focal length of the camera.
        - ``horizontal_aperture``: The horizontal aperture of the camera.
        - ``vertical_aperture``: The vertical aperture of the camera.
        - ``horizontal_aperture_offset``: The horizontal offset of the camera.
        - ``vertical_aperture_offset``: The vertical offset of the camera.

        .. warning::

            Due to limitations of Omniverse camera, we need to assume that the camera is a spherical lens,
            i.e. has square pixels, and the optical center is centered at the camera eye. If this assumption
            is not true in the input intrinsic matrix, then the camera will not set up correctly.

        Args:
            matrices: The intrinsic matrices for the camera. Shape is (N, 3, 3).
            focal_length: Focal length to use when computing aperture values. Defaults to 1.0.
            env_ids: A sensor ids to manipulate. Defaults to None, which means all sensor indices.
        """
        # resolve env_ids
        if env_ids is None:
            env_ids = self._ALL_INDICES
        # iterate over env_ids
        for i, matrix in zip(env_ids, matrices):
            # convert to numpy for sanity
            intrinsic_matrix = np.asarray(matrix, dtype=float)
            # extract parameters from matrix
            f_x = intrinsic_matrix[0, 0]
            c_x = intrinsic_matrix[0, 2]
            f_y = intrinsic_matrix[1, 1]
            c_y = intrinsic_matrix[1, 2]
            # get viewport parameters
            height, width = self.image_shape
            height, width = float(height), float(width)
            # resolve parameters for usd camera
            params = {
                "focal_length": focal_length,
                "horizontal_aperture": width * focal_length / f_x,
                "vertical_aperture": height * focal_length / f_y,
                "horizontal_aperture_offset": (c_x - width / 2) / f_x,
                "vertical_aperture_offset": (c_y - height / 2) / f_y,
            }
            # change data for corresponding camera index
            sensor_prim = self._sensor_prims[i]
            # set parameters for camera
            for param_name, param_value in params.items():
                # convert to camel case (CC)
                param_name = to_camel_case(param_name, to="CC")
                # get attribute from the class
                param_attr = getattr(sensor_prim, f"Get{param_name}Attr")
                # set value
                # note: We have to do it this way because the camera might be on a different
                #   layer (default cameras are on session layer), and this is the simplest
                #   way to set the property on the right layer.
                omni.usd.set_prop_val(param_attr(), param_value)

    """
    Operations - Set pose.
    """

    def set_world_poses(
        self,
        positions: torch.Tensor | None = None,
        orientations: torch.Tensor | None = None,
        env_ids: Sequence[int] | None = None,
        convention: Literal["opengl", "ros", "world"] = "ros",
    ):
        r"""Set the pose of the camera w.r.t. the world frame using specified convention.

        Since different fields use different conventions for camera orientations, the method allows users to
        set the camera poses in the specified convention. Possible conventions are:

        - :obj:`"opengl"` - forward axis: -Z - up axis +Y - Offset is applied in the OpenGL (Usd.Camera) convention
        - :obj:`"ros"`    - forward axis: +Z - up axis -Y - Offset is applied in the ROS convention
        - :obj:`"world"`  - forward axis: +X - up axis +Z - Offset is applied in the World Frame convention

        See :meth:`omni.isaac.orbit.sensors.camera.utils.convert_orientation_convention` for more details
        on the conventions.

        Args:
            positions: The cartesian coordinates (in meters). Shape is (N, 3).
                Defaults to None, in which case the camera position in not changed.
            orientations: The quaternion orientation in (w, x, y, z). Shape is (N, 4).
                Defaults to None, in which case the camera orientation in not changed.
            env_ids: A sensor ids to manipulate. Defaults to None, which means all sensor indices.
            convention: The convention in which the poses are fed. Defaults to "ros".

        Raises:
            RuntimeError: If the camera prim is not set. Need to call :meth:`initialize` method first.
        """
        # resolve env_ids
        if env_ids is None:
            env_ids = self._ALL_INDICES
        # convert to backend tensor
        if positions is not None:
            if isinstance(positions, np.ndarray):
                positions = torch.from_numpy(positions).to(device=self._device)
            elif not isinstance(positions, torch.Tensor):
                positions = torch.tensor(positions, device=self._device)
        # convert rotation matrix from input convention to OpenGL
        if orientations is not None:
            if isinstance(orientations, np.ndarray):
                orientations = torch.from_numpy(orientations).to(device=self._device)
            elif not isinstance(orientations, torch.Tensor):
                orientations = torch.tensor(orientations, device=self._device)
            orientations = convert_orientation_convention(orientations, origin=convention, target="opengl")
        # set the pose
        self._view.set_world_poses(positions, orientations, env_ids)

    def set_world_poses_from_view(
        self, eyes: torch.Tensor, targets: torch.Tensor, env_ids: Sequence[int] | None = None
    ):
        """Set the poses of the camera from the eye position and look-at target position.

        Args:
            eyes: The positions of the camera's eye. Shape is (N, 3).
            targets: The target locations to look at. Shape is (N, 3).
            env_ids: A sensor ids to manipulate. Defaults to None, which means all sensor indices.

        Raises:
            RuntimeError: If the camera prim is not set. Need to call :meth:`initialize` method first.
            NotImplementedError: If the stage up-axis is not "Y" or "Z".
        """
        # resolve env_ids
        if env_ids is None:
            env_ids = self._ALL_INDICES
        # set camera poses using the view
        orientations = quat_from_matrix(create_rotation_matrix_from_view(eyes, targets, device=self._device))
        self._view.set_world_poses(eyes, orientations, env_ids)

    """
    Operations
    """

    def reset(self, env_ids: Sequence[int] | None = None):
        # reset the timestamps
        super().reset(env_ids)
        # resolve None
        # note: cannot do smart indexing here since we do a for loop over data.
        if env_ids is None:
            env_ids = self._ALL_INDICES
        # reset the data
        # note: this recomputation is useful if one performs randomization on the camera poses.
        self._update_poses(env_ids)
        self._update_intrinsic_matrices(env_ids)
        # Reset the frame count
        self._frame[env_ids] = 0

    """
    Implementation.
    """

    def _initialize_impl(self):
        """Initializes the sensor handles and internal buffers.

        This function creates handles and registers the provided data types with the replicator registry to
        be able to access the data from the sensor. It also initializes the internal buffers to store the data.

        Raises:
            RuntimeError: If the number of camera prims in the view does not match the number of environments.
        """
        import omni.replicator.core as rep

        # Initialize parent class
        super()._initialize_impl()
        # Create a view for the sensor
        self._view = XFormPrimView(self.cfg.prim_path, reset_xform_properties=False)
        self._view.initialize()
        # Check that sizes are correct
        if self._view.count != self._num_envs:
            raise RuntimeError(
                f"Number of camera prims in the view ({self._view.count}) does not match"
                f" the number of environments ({self._num_envs})."
            )

        # Create all env_ids buffer
        self._ALL_INDICES = torch.arange(self._view.count, device=self._device, dtype=torch.long)
        # Create frame count buffer
        self._frame = torch.zeros(self._view.count, device=self._device, dtype=torch.long)

        # Attach the sensor data types to render node
        self._render_product_paths: list[str] = list()
        self._rep_registry: dict[str, list[rep.annotators.Annotator]] = {name: list() for name in self.cfg.data_types}
        # Resolve device name
        if "cuda" in self._device:
            device_name = self._device.split(":")[0]
        else:
            device_name = "cpu"
        # Obtain current stage
        stage = omni.usd.get_context().get_stage()
        # Convert all encapsulated prims to Camera
        for cam_prim_path in self._view.prim_paths:
            # Get camera prim
            cam_prim = stage.GetPrimAtPath(cam_prim_path)
            # Check if prim is a camera
            if not cam_prim.IsA(UsdGeom.Camera):
                raise RuntimeError(f"Prim at path '{cam_prim_path}' is not a Camera.")
            # Add to list
            sensor_prim = UsdGeom.Camera(cam_prim)
            self._sensor_prims.append(sensor_prim)
            # Get render product
            # From Isaac Sim 2023.1 onwards, render product is a HydraTexture so we need to extract the path
            render_prod_path = rep.create.render_product(cam_prim_path, resolution=(self.cfg.width, self.cfg.height))
            if not isinstance(render_prod_path, str):
                render_prod_path = render_prod_path.path
            self._render_product_paths.append(render_prod_path)
            # Iterate over each data type and create annotator
            # TODO: This will move out of the loop once Replicator supports multiple render products within a single
            #  annotator, i.e.: rep_annotator.attach(self._render_product_paths)
            for name in self.cfg.data_types:
                # init params -- Checked from rep.scripts.writes_default.basic_writer.py
                # note: we are verbose here to make it easier to understand the code.
                #   if colorize is true, the data is mapped to colors and a uint8 4 channel image is returned.
                #   if colorize is false, the data is returned as a uint32 image with ids as values.
                if name in ["bounding_box_2d_tight", "bounding_box_2d_loose", "bounding_box_3d"]:
                    init_params = {"semanticTypes": self.cfg.semantic_types}
                elif name in ["semantic_segmentation", "instance_segmentation"]:
                    init_params = {"semanticTypes": self.cfg.semantic_types, "colorize": self.cfg.colorize}
                elif name in ["instance_id_segmentation"]:
                    init_params = {"colorize": self.cfg.colorize}
                else:
                    init_params = None
                # create annotator node
                rep_annotator = rep.AnnotatorRegistry.get_annotator(name, init_params, device=device_name)
                rep_annotator.attach(render_prod_path)
                # add to registry
                self._rep_registry[name].append(rep_annotator)
        # Create internal buffers
        self._create_buffers()

    def _update_buffers_impl(self, env_ids: Sequence[int]):
        # Increment frame count
        self._frame[env_ids] += 1
        # -- intrinsic matrix
        self._update_intrinsic_matrices(env_ids)
        # -- pose
        self._update_poses(env_ids)
        # -- read the data from annotator registry
        # check if buffer is called for the first time. If so then, allocate the memory
        if len(self._data.output.sorted_keys) == 0:
            # this is the first time buffer is called
            # it allocates memory for all the sensors
            self._create_annotator_data()
        else:
            # iterate over all the data types
            for name, annotators in self._rep_registry.items():
                # iterate over all the annotators
                for index in env_ids:
                    # get the output
                    output = annotators[index].get_data()
                    # process the output
                    data, info = self._process_annotator_output(output)
                    # add data to output
                    self._data.output[name][index] = data
                    # add info to output
                    self._data.info[index][name] = info

    """
    Private Helpers
    """

    def _check_supported_data_types(self, cfg: CameraCfg):
        """Checks if the data types are supported by the ray-caster camera."""
        # check if there is any intersection in unsupported types
        # reason: these use np structured data types which we can't yet convert to torch tensor
        common_elements = set(cfg.data_types) & Camera.UNSUPPORTED_TYPES
        if common_elements:
            raise ValueError(
                f"Camera class does not support the following sensor types: {common_elements}."
                "\n\tThis is because these sensor types output numpy structured data types which"
                "can't be converted to torch tensors easily."
                "\n\tHint: If you need to work with these sensor types, we recommend using the single camera"
                " implementation from the omni.isaac.orbit.compat.camera module."
            )

    def _create_buffers(self):
        """Create buffers for storing data."""
        # create the data object
        # -- pose of the cameras
        self._data.pos_w = torch.zeros((self._view.count, 3), device=self._device)
        self._data.quat_w_world = torch.zeros((self._view.count, 4), device=self._device)
        # -- intrinsic matrix
        self._data.intrinsic_matrices = torch.zeros((self._view.count, 3, 3), device=self._device)
        self._data.image_shape = self.image_shape
        # -- output data
        # lazy allocation of data dictionary
        # since the size of the output data is not known in advance, we leave it as None
        # the memory will be allocated when the buffer() function is called for the first time.
        self._data.output = TensorDict({}, batch_size=self._view.count, device=self.device)
        self._data.info = [{name: None for name in self.cfg.data_types} for _ in range(self._view.count)]

    def _update_intrinsic_matrices(self, env_ids: Sequence[int]):
        """Compute camera's matrix of intrinsic parameters.

        Also called calibration matrix. This matrix works for linear depth images. We assume square pixels.

        Note:
            The calibration matrix projects points in the 3D scene onto an imaginary screen of the camera.
            The coordinates of points on the image plane are in the homogeneous representation.
        """
        # iterate over all cameras
        for i in env_ids:
            # Get corresponding sensor prim
            sensor_prim = self._sensor_prims[i]
            # get camera parameters
            focal_length = sensor_prim.GetFocalLengthAttr().Get()
            horiz_aperture = sensor_prim.GetHorizontalApertureAttr().Get()
            # get viewport parameters
            height, width = self.image_shape
            # calculate the field of view
            fov = 2 * math.atan(horiz_aperture / (2 * focal_length))
            # calculate the focal length in pixels
            focal_px = width * 0.5 / math.tan(fov / 2)
            # create intrinsic matrix for depth linear
            self._data.intrinsic_matrices[i, 0, 0] = focal_px
            self._data.intrinsic_matrices[i, 0, 2] = width * 0.5
            self._data.intrinsic_matrices[i, 1, 1] = focal_px
            self._data.intrinsic_matrices[i, 1, 2] = height * 0.5
            self._data.intrinsic_matrices[i, 2, 2] = 1

    def _update_poses(self, env_ids: Sequence[int]):
        """Computes the pose of the camera in the world frame with ROS convention.

        This methods uses the ROS convention to resolve the input pose. In this convention,
        we assume that the camera front-axis is +Z-axis and up-axis is -Y-axis.

        Returns:
            A tuple of the position (in meters) and quaternion (w, x, y, z).
        """
        # check camera prim exists
        if len(self._sensor_prims) == 0:
            raise RuntimeError("Camera prim is None. Please call 'sim.play()' first.")

        # get the poses from the view
        poses, quat = self._view.get_world_poses(env_ids)
        self._data.pos_w[env_ids] = poses
        self._data.quat_w_world[env_ids] = convert_orientation_convention(quat, origin="opengl", target="world")

    def _create_annotator_data(self):
        """Create the buffers to store the annotator data.

        We create a buffer for each annotator and store the data in a dictionary. Since the data
        shape is not known beforehand, we create a list of buffers and concatenate them later.

        This is an expensive operation and should be called only once.
        """
        # add data from the annotators
        for name, annotators in self._rep_registry.items():
            # create a list to store the data for each annotator
            data_all_cameras = list()
            # iterate over all the annotators
            for index in self._ALL_INDICES:
                # get the output
                output = annotators[index].get_data()
                # process the output
                data, info = self._process_annotator_output(output)
                # append the data
                data_all_cameras.append(data)
                # store the info
                self._data.info[index][name] = info
            # concatenate the data along the batch dimension
            self._data.output[name] = torch.stack(data_all_cameras, dim=0)

    def _process_annotator_output(self, output: Any) -> tuple[torch.tensor, dict]:
        """Process the annotator output.

        This function is called after the data has been collected from all the cameras.
        """
        # extract info and data from the output
        if isinstance(output, dict):
            data = output["data"]
            info = output["info"]
        else:
            data = output
            info = None
        # convert data into torch tensor
        data = convert_to_torch(data, device=self.device)
        # return the data and info
        return data, info

    """
    Internal simulation callbacks.
    """

    def _invalidate_initialize_callback(self, event):
        """Invalidates the scene elements."""
        # call parent
        super()._invalidate_initialize_callback(event)
        # set all existing views to None to invalidate them
        self._view = None
