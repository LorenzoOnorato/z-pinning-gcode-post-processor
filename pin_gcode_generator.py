"""
pin_gcode_generator.py
----------------------
Generates the per-layer pinning G-code that is later spliced into the base print by
GCodeModifier.

Core class: ``GCodeCommandsComposer``

The composer takes the pin layout (from PinDefinition), the positions of all parts on
the build plate, and a comprehensive set of printing parameters.  It then builds a
dictionary mapping each layer number to a list of G-code commands that:

  1. Travel to each pin location (with Z-hop and retraction).
  2. Dive the nozzle into the previously deposited material (``diving_mode=True``) or
     approach from the top (``diving_mode=False``).
  3. Extrude the pin with optional features:
       - Variable extrusion (``extrusion_skew_percentage``) — ramps extrusion along the pin.
       - Geometrical extrusion (``geometrical_extrusion_enabled``) — shapes the pin as a
         rivet: lower cone → cylinder → upper cone.
       - Wipe move after retraction to reduce stringing.
       - Staggered start layers so adjacent pins do not begin at the same height.
  4. Retract and return the nozzle to the safe travel height.

The output of ``compose_layer_gcode()`` is passed directly to
``GCodeModifier.insert_pin_gcode()``.
"""
import math
from gcode_injector import parse_gcode_lines
import csv
from pathlib import Path
from typing import Optional


def _apply_transformation(pin_positions, translation_xy, rotation_angle, cross_section_x_dim, cross_section_y_dim):
    """
    Apply translation and rotation transformations to the pin positions, considering the center of the specimen.

    Args:
        pin_positions (list): List of tuples representing the original pin positions (X, Y).
        translation_xy (tuple): XY translation values as (x_translation, y_translation).
        rotation_angle (float): Rotation angle in degrees.
        cross_section_x_dim (float): Dimension of the cross-section oriented in the x direction after rotation.

    Returns:
        list: Transformed pin positions.
    """
    x_translation, y_translation = translation_xy
    angle_rad = math.radians(rotation_angle)
    cos_theta = math.cos(angle_rad)
    sin_theta = math.sin(angle_rad)

    # Adjust the x_translation by half of the cross-section dimension in the x direction
    x_translation -= (cross_section_x_dim / 2) * cos_theta - (cross_section_y_dim / 2) * sin_theta
    y_translation -= (cross_section_x_dim / 2) * sin_theta + (cross_section_y_dim / 2) * cos_theta

    transformed_positions = []
    for x, y in pin_positions:
        # Apply rotation
        x_rotated = x * cos_theta - y * sin_theta
        y_rotated = x * sin_theta + y * cos_theta

        # Apply translation
        x_transformed = x_rotated + x_translation
        y_transformed = y_rotated + y_translation

        transformed_positions.append((x_transformed, y_transformed))

    return transformed_positions


class GCodeCommandsComposer:
    """
    Composes per-layer pinning G-code from a pin layout and a set of printing parameters.

    Use ``compose_layer_gcode()`` to obtain the layer→G-code mapping that is passed to
    ``GCodeModifier.insert_pin_gcode()``.
    """

    FILAMENT_DIAMETER = 1.75  # mm — standard 1.75 mm FDM filament
    NORMAL_PRINTING_TEMPERATURE = 315  # Default return temperature; override via a constructor parameter if needed.

    def __init__(self, pin_data: dict, parts_dict: list, specimen_height_mm: float,
                 flow_ratio: float,
                 # Travel parameters
                 z_lift_speed: float, xy_travel_speed: float, z_hop_length: float,
                 retraction_length: float, z_drop_speed: float,
                 # Extrusion parameters
                 pinning_extrusion_speed: float,
                 # Nozzle mode
                 diving_mode: bool, spiral_mode: bool, nozzle_outer_diameter: float,
                 rib_inside_protrusion: float, rib_clearance: float,
                 nozzle_sinking: float, nozzle_sinking_speed: float, nozzle_sinking_wait_time: float,
                 nozzle_sinking_1st_layer: bool, nozzle_extrude_sunk: bool,
                 # Wipe
                 wipe_enabled: bool, wipe_speed: float,
                 # Variable extrusion
                 variable_extrusion_enabled: bool, extrusion_skew_percentage: float,
                 # Geometrical extrusion / rivet shape
                 geometrical_extrusion_enabled: bool, cone_blob: bool, blob_feedrate: float,
                 # Retraction / pressure
                 no_pin_retraction: bool, pressure_E_length: float, pressure_E_speed: float,
                 # Optional
                 heated_pin: "float | bool" = False, stagger_params: Optional[dict] = None,
                 pin_rivet_parameters: Optional[dict] = None):
        """
        Args:
            pin_data (dict): Output of ``PinDefinition.define_pins_relative_xy()``.
                Contains pin positions, cross-section dimensions, pin height, and layer height.
            parts_dict (list[dict]): List of part descriptors, each with keys:
                ``'name'`` (str), ``'xy'`` (tuple[float, float]) — build-plate position in mm,
                ``'rotation'`` (float) — rotation angle in degrees.
            specimen_height_mm (float): Total height of the printed specimen in mm.
            flow_ratio (float): Extrusion multiplier (e.g. ``1.65`` means 165 % of the
                theoretical extrusion volume).

            z_lift_speed (float): Z-axis travel speed during hops (mm/min).
            xy_travel_speed (float): XY travel speed between pins (mm/min).
            z_hop_length (float): Z-hop height above the print surface before travelling (mm).
            retraction_length (float): Filament retraction distance before a travel move (mm).
            z_drop_speed (float): Speed at which the nozzle descends to the pin entry point (mm/min).

            pinning_extrusion_speed (float): Feedrate used while extruding the pin (mm/min).

            diving_mode (bool): If ``True``, the nozzle dives *into* the deposited layers to
                form the pin from below.  If ``False``, the pin is built from the top surface.
            spiral_mode (bool): If ``True``, the nozzle traces a spiral path during diving
                (only active when ``diving_mode=True``).
            nozzle_outer_diameter (float): Outer diameter of the nozzle tip (mm), used for
                spiral and rib calculations.
            rib_inside_protrusion (float): Axial protrusion of the inner rib geometry (mm).
            rib_clearance (float): Radial clearance between rib and hole wall (mm).
            nozzle_sinking (float): Additional depth the nozzle sinks below the layer surface (mm).
            nozzle_sinking_speed (float): Speed of the nozzle sinking move (mm/min).
            nozzle_sinking_wait_time (float): Dwell time at the bottom of the sinking move (s).
            nozzle_sinking_1st_layer (bool): If ``True``, apply sinking on the very first
                pinning layer as well.
            nozzle_extrude_sunk (bool): If ``True``, extrude filament while the nozzle is
                sunk (before rising).

            wipe_enabled (bool): If ``True``, add a short wipe move after the final retraction
                to reduce stringing.
            wipe_speed (float): Feedrate for the wipe move (mm/min).

            variable_extrusion_enabled (bool): If ``True``, ramp the extrusion rate along the
                pin height using ``extrusion_skew_percentage``.
            extrusion_skew_percentage (float): Peak-to-base extrusion ratio expressed as a
                percentage (100 = uniform; 200 = doubles at the top).

            geometrical_extrusion_enabled (bool): If ``True``, shape the pin as a rivet
                (lower cone → cylinder → upper cone) using ``pin_rivet_parameters``.
            cone_blob (bool): If ``True``, add a blob of material at the pin tip for the cone
                shape.
            blob_feedrate (float): Feedrate used for the cone blob extrusion (mm/min).

            no_pin_retraction (bool): If ``True``, skip the filament retraction before
                travelling to the next pin.
            pressure_E_length (float): Extra filament length pushed before the pin extrusion
                starts, to pre-pressurise the nozzle (mm).  Set to ``0`` to disable.
            pressure_E_speed (float): Feedrate for the pressure-advance move (mm/min).

            heated_pin (float | bool): If a temperature (°C) is given, the nozzle is heated
                to that temperature before each pin and cooled back afterwards.
                ``False`` disables heated-pin mode.
            stagger_params (dict | None): Controls the staggered start layers of individual
                pins.  Accepted keys:
                  - ``'fixed_start_layers'`` (list[int]): explicit start layer for each pin index.
                Set to ``None`` for uniform start layers.
            pin_rivet_parameters (dict | None): Geometry of the rivet shape (used when
                ``geometrical_extrusion_enabled=True``).  Keys:
                  - ``'cone_radius'`` (float, mm)
                  - ``'cone_height'`` (float, mm)
                  - ``'cylinder_radius'`` (float, mm)
                  - ``'cylinder_height'`` (float, mm)
        """

        # FROM pin_layout.py
        self.pin_positions = pin_data["pins_relative_xy"]

        # FROM pin_layout.py (itself from dashboard.py)
        self.largest_side = pin_data["largest_side"]
        self.smallest_side = pin_data["smallest_side"]
        self.pin_height_mm = pin_data["pin_height_mm"]
        self.pin_dimension = pin_data["pin_dimension"]
        self.layer_height = pin_data["layer_height"]

        # FROM dashboard.py
        self.parts_dict = parts_dict
        self.specimen_height_mm = specimen_height_mm
        self.flow_ratio = flow_ratio

        self.z_lift_speed = z_lift_speed
        self.xy_travel_speed = xy_travel_speed
        self.z_hop_length = z_hop_length
        self.retraction_length = retraction_length  # Amount to retract filament before moving
        self.z_drop_speed = z_drop_speed
        self.pinning_extrusion_speed = pinning_extrusion_speed
        self.rib_clearance = rib_clearance
        self.wipe_speed = wipe_speed

        self.diving_mode = diving_mode
        self.heated_pin = heated_pin
        self.spiral_mode = spiral_mode
        self.nozzle_outer_diameter = nozzle_outer_diameter
        self.rib_inside_protrusion = rib_inside_protrusion
        self.nozzle_sinking = nozzle_sinking
        self.nozzle_sinking_speed = nozzle_sinking_speed
        self.nozzle_sinking_wait_time = nozzle_sinking_wait_time
        self.wipe_enabled = wipe_enabled
        self.nozzle_sinking_1st_layer = nozzle_sinking_1st_layer
        self.variable_extrusion_enabled = variable_extrusion_enabled
        self.extrusion_skew_percentage = extrusion_skew_percentage
        self.nozzle_extrude_sunk = nozzle_extrude_sunk
        self.stagger_params = stagger_params
        self.pin_rivet_parameters = pin_rivet_parameters

        # further extrusion tricks
        self.geometrical_extrusion_enabled = geometrical_extrusion_enabled
        self.cone_blob = cone_blob
        self.blob_feedrate = blob_feedrate
        self.no_pin_retraction = no_pin_retraction
        self.pressure_E_length = pressure_E_length
        self.pressure_E_speed = pressure_E_speed

        # calculated
        self.total_layers = int(self.specimen_height_mm / self.layer_height)
        if self.pin_height_mm > 0 and self.layer_height > 0:
            self.pin_height_layers = int(self.pin_height_mm / self.layer_height)
        else:
            self.pin_height_layers = 0  # Handle the case where pin height or layer height is invalid

        self.pins_absolute_xy_per_part = {
            part['name']: _apply_transformation(self.pin_positions, part['xy'], part['rotation'], self.largest_side,
                                                self.smallest_side)
            for part in self.parts_dict}

        self.pins_absolute_xy = None

    def _generate_staggered_pinning_schedule(self):
        """
        Generate a staggered pinning schedule based on the pin height and the number of layers in the specimen.

        Returns:
            dict[int, list[dict]]: Staggered pinning schedule. Keys are layer numbers (int).
                Each value is a list of pin-action dicts with keys:
                  - ``'pin_index'`` (int): Index of the pin in ``self.pin_positions``.
                  - ``'height_layers'`` (int): Height of the pin at this layer in layers.
                  - ``'structure'`` (list): Pin structure as returned by
                    ``_determine_pin_structure()``, or ``['cylinder']`` when geometrical
                    extrusion is disabled.
        """
        staggered_schedule = {}  # Output schedule
        pin_height_layers = self.pin_height_layers  # Number of layers each pin spans

        # Initialize staggering parameters
        fixed_start_layers = self.stagger_params.get("fixed_start_layers", None) if self.stagger_params else None

        if self.stagger_params and not fixed_start_layers:
            # stagger_params was provided but contained no usable fixed_start_layers key
            print("Warning: stagger_params provided but 'fixed_start_layers' key is missing — all pins will start at layer 0.")

        # Iterate through pins
        for pin_idx in range(len(self.pin_positions)):
            # Determine the start layer
            if fixed_start_layers:
                start_layer = fixed_start_layers[pin_idx % len(fixed_start_layers)]
            else:
                start_layer = 0

            # Iterate through the layers for this pin
            for layer in range(start_layer, self.total_layers + pin_height_layers, pin_height_layers):
                if layer == start_layer:
                    pin_height = start_layer
                    schedule_layer = layer
                elif layer >= self.total_layers:
                    pin_height = pin_height_layers - (layer - self.total_layers)
                    schedule_layer = self.total_layers
                else:
                    pin_height = pin_height_layers
                    schedule_layer = layer

                if self.geometrical_extrusion_enabled:
                    pin_structure = self._determine_pin_structure(pin_height, schedule_layer)
                else:
                    pin_structure = ['cylinder']

                if schedule_layer not in staggered_schedule:
                    staggered_schedule[schedule_layer] = []
                staggered_schedule[schedule_layer].append({
                    "pin_index": pin_idx,
                    "height_layers": pin_height,
                    "structure": pin_structure
                })
        return staggered_schedule

    def _determine_pin_structure(self, pin_height, layer):
        """
        Decompose a pin of *pin_height* layers into its rivet-shaped segments.

        Args:
            pin_height (int): Pin height in layers.
            layer (int): Layer number at which this pin starts. Used to detect whether
                this is a partial bottom pin (layer < total pin height layers).

        Returns:
            list[tuple[str, float]]: Ordered segments bottom-to-top. Each element is
                ``(section_name, height_mm)`` where section_name is one of
                ``'lower_cone'``, ``'cylinder'``, or ``'upper_cone'``.
                Zero-height segments are excluded.
        """
        assert self.pin_rivet_parameters is not None
        cone_height = self.pin_rivet_parameters["cone_height"]
        cylinder_height = self.pin_rivet_parameters["cylinder_height"] * 2

        pin_structure = []
        remaining_height = pin_height * self.layer_height

        adjust_lower_cone = False
        adjust_cylinder = False
        adjusted_cone_height = 0
        adjusted_cylinder_height = 0

        if layer < self.pin_height_layers:
            adjust_lower_cone = True
            adjusted_cone_height = remaining_height - cone_height - cylinder_height
            if adjusted_cone_height < 0:
                adjusted_cone_height = 0
                adjust_cylinder = True
                adjusted_cylinder_height = remaining_height - cone_height
                if adjusted_cylinder_height < 0:
                    adjusted_cylinder_height = 0

        if remaining_height > 0:
            if remaining_height >= cone_height and not adjust_lower_cone:
                pin_structure.append(("lower_cone", round(cone_height, 3)))
                remaining_height -= cone_height
            elif adjust_lower_cone:
                pin_structure.append(("lower_cone", round(adjusted_cone_height, 3)))
                remaining_height -= adjusted_cone_height
            else:
                pin_structure.append(("lower_cone", round(remaining_height, 3)))
                remaining_height = 0

        if remaining_height > 0:
            if remaining_height >= cylinder_height and not adjust_cylinder:
                pin_structure.append(("cylinder", round(cylinder_height, 3)))
                remaining_height -= cylinder_height
            elif adjust_cylinder:
                pin_structure.append(("cylinder", round(adjusted_cylinder_height, 3)))
                remaining_height -= adjusted_cylinder_height
            else:
                pin_structure.append(("cylinder", round(remaining_height, 3)))
                remaining_height = 0
        else:
            pin_structure.append(("cylinder", 0))

        if remaining_height > 0:
            if remaining_height >= cone_height:
                pin_structure.append(("upper_cone", round(cone_height, 3)))
                remaining_height -= cone_height
            else:
                pin_structure.append(("upper_cone", round(remaining_height, 3)))
                remaining_height = 0
        else:
            pin_structure.append(("upper_cone", 0))

        if remaining_height > 0:
            print(f"Warning: pin structure has {remaining_height:.3f} mm unaccounted height — check pin_rivet_parameters.")

        pin_structure = [(name, h) for name, h in pin_structure if h > 0]

        return pin_structure

    def compose_layer_gcode(self) -> tuple:
        """
        Build the complete per-layer pinning G-code for all parts on the build plate.

        Returns:
            tuple[dict, dict]:
                - ``gcode_lines`` (dict[int, list]): Maps layer numbers (as ints, matching
                  the Z-based layer index in the parsed G-code) to a list of G-code command
                  strings to be inserted at that layer boundary.
                - ``constants`` (dict): Key printing parameters logged as a header block in the
                  output G-code file, including ``pin_height_layers``, ``flow_ratio``,
                  ``layer_height``, ``diving_mode``, nozzle settings, and extrusion flags.
        """
        # Generate the staggered pinning schedule
        staggered_schedule = self._generate_staggered_pinning_schedule()
        gcode_lines_per_layer = {}

        for layer in range(1, self.total_layers + 1):
            if layer not in staggered_schedule:
                continue
            gcode_lines = []
            gcode_lines.append(f";--- PINNING LAYER {layer} (Z = {layer * self.layer_height}) ---")
            gcode_lines.append(f"M83 ; relative extrusion mode")
            gcode_lines.append("")

            if self.heated_pin is not False:
                gcode_lines.extend([
                    f";- HEATING NOZZLE -",
                    f"M104 S{self.heated_pin} ; set pin temperature",
                    f"M109 S{self.heated_pin} ; wait for it",
                    ""
                ])

            # Process pinning actions for this layer for each part
            for part_name, part_pins_absolute_xy in self.pins_absolute_xy_per_part.items():
                gcode_lines.append(f";- PINNING PART {part_name} -")
                for pin in staggered_schedule[layer]:
                    x, y = part_pins_absolute_xy[pin["pin_index"]]
                    gcode_lines.extend(
                        self._generate_pin_gcode(x, y, layer, pin["pin_index"], pin["height_layers"], pin["structure"]))

            if self.heated_pin is not False:
                gcode_lines.extend([
                    f";- COOLING NOZZLE -",
                    f"M104 S{self.NORMAL_PRINTING_TEMPERATURE} ; back to printing temperature",
                    f"M109 S{self.NORMAL_PRINTING_TEMPERATURE} ; wait for it",
                    ""
                ])

            gcode_lines.append(f"M82 ; absolute extrusion mode")
            gcode_lines.append(f";--- END OF PINNING LAYER {layer} ---")
            gcode_lines.append("")

            # Store the generated G-code for this layer
            gcode_lines_per_layer[layer] = parse_gcode_lines(gcode_lines, self.layer_height)

        # Prepare constants for debugging or reference
        constants = {
            "pin_positions": self.pin_positions,
            "staggered_schedule": staggered_schedule,
            "pin_height_mm": self.pin_height_mm,
            "pin_dimension": self.pin_dimension,
            "layer_height": self.layer_height,
            "pin_height_layers": self.pin_height_layers,
            "total_layers": self.total_layers,
            "retract_amount": self.retraction_length,
            "z_lift": self.z_hop_length,
            "xy_speed": self.xy_travel_speed,
            "z_drop_speed": self.z_drop_speed,
            "z_lift_speed": self.z_lift_speed,
            "parts_dict": self.parts_dict,
            "diving_mode": self.diving_mode,
            "heated_pin": self.heated_pin,
            "spiral_mode": self.spiral_mode,
            "nozzle_outer_diameter": self.nozzle_outer_diameter,
            "rib_inside_protrusion": self.rib_inside_protrusion,
            "rib_clearance": self.rib_clearance,
            "nozzle_sinking": self.nozzle_sinking,
            "nozzle_sinking_speed": self.nozzle_sinking_speed,
            "nozzle_sinking_wait_time": self.nozzle_sinking_wait_time,
            "nozzle_sinking_1st_layer": self.nozzle_sinking_1st_layer,
            "wipe_enabled": self.wipe_enabled,
            "wipe_speed": self.wipe_speed,
            "variable_extrusion_enabled": self.variable_extrusion_enabled,
            "extrusion_skew_percentage": self.extrusion_skew_percentage,
            "flow_ratio": self.flow_ratio,
            "geometrical_extrusion_enabled": self.geometrical_extrusion_enabled,
            "cone_blob": self.cone_blob,
            "blob_feedrate": self.blob_feedrate,
            "no_pin_retraction": self.no_pin_retraction,
            "pressure_E_length": self.pressure_E_length,
            "pressure_E_speed": self.pressure_E_speed
        }

        print(f"\n[Step 2] G-code composition")
        print(f"  {self.total_layers} layers  |  {len(self.pin_positions)} pin(s) per part  |  {len(self.parts_dict)} part(s)")
        print(f"  G-code composed successfully.")

        return gcode_lines_per_layer, constants

    def _generate_pin_gcode(self, x, y, layer, idx, current_pin_height, pin_structure):
        """
        Generate the G-code command list for a single pin at (x, y).

        Args:
            x (float): Absolute X position of the pin centre on the build plate (mm).
            y (float): Absolute Y position of the pin centre on the build plate (mm).
            layer (int): Layer number at which this pin is inserted.
            idx (int): Zero-based pin index (used for human-readable G-code comments only).
            current_pin_height (int): Pin height in layers (converted to mm internally).
            pin_structure (list[tuple[str, float]]): Output of ``_determine_pin_structure()``.
                Each element is ``(section_name, height_mm)``: ``'lower_cone'``,
                ``'cylinder'``, or ``'upper_cone'``.

        Returns:
            list[str]: G-code lines to be inserted at this layer.

        Note:
            ``one_shot``, ``smooth_depressurizing``, and ``gcode_commands_per_layer`` are
            reserved for future parameterisation and are currently hard-coded to
            False, False, and 1 respectively.
        """

        # Reserved for future parameterisation; currently unused.
        gcode_commands_per_layer = 1
        smooth_depressurizing = False
        one_shot = False

        pin_layer_z = self.layer_height * layer
        z = pin_layer_z
        current_pin_height *= self.layer_height
        step_height = self.layer_height / gcode_commands_per_layer

        tot_E_pin = 0.0
        if self.pin_rivet_parameters:
            tot_E_pin = self._rivet_like_pin_extrusion_length(current_pin_height)
        elif not self.geometrical_extrusion_enabled:
            tot_E_pin = self.pin_dimension ** 2 / self.FILAMENT_DIAMETER ** 2 * current_pin_height * self.flow_ratio
        else:
            raise ValueError("Pin geometry not provided. Check pin_rivet_parameters and geometrical_extrusion_enabled.")

        gcode_lines = []
        gcode_lines.append(f"; Pin {idx + 1} (X{x:.3f} Y{y:.3f} Z{z:.3f})")
        gcode_lines.append(f"M117 Pin {idx + 1} at layer {layer}")
        gcode_lines.append(f"G1 X{x:.3f} Y{y:.3f} F{self.xy_travel_speed} ; MOVE TO XY")

        if self.diving_mode:
            z -= current_pin_height
            gcode_lines.append(
                f"G1 Z{z:.3f} F{self.z_drop_speed} ; DROP Z to the bottom of the pin")

        # Skip sinking when the pin bottom is too close to the bed (z ≤ sinking depth),
        # unless nozzle_sinking_1st_layer explicitly enables it on the first layer.
        if self.nozzle_sinking and (z > self.nozzle_sinking or self.nozzle_sinking_1st_layer):
            z -= self.nozzle_sinking
            gcode_lines.append(f"G1 Z{z:.3f} F{self.nozzle_sinking_speed} ; SINKING NOZZLE")
            if self.nozzle_sinking_wait_time:
                gcode_lines.append(f"G4 P{self.nozzle_sinking_wait_time * 1000} ; WAIT")
            if not self.nozzle_extrude_sunk:
                z += self.nozzle_sinking
                gcode_lines.append(f"G1 Z{z:.3f} F{self.nozzle_sinking_speed} ; LIFTING NOZZLE")

        gcode_lines.append(f"; EXTRUDING PIN")

        if self.pressure_E_length and current_pin_height == self.pin_height_mm and not one_shot:
            gcode_lines.append(f"G1 Z{z:.2f} E{self.pressure_E_length:.4f} F{self.pressure_E_speed} ; pressurizing")

        E_layers = int(current_pin_height / self.layer_height)
        if E_layers == 0:
            return []  # nothing to extrude for a zero-height pin
        gcode_command_extrusion_length = tot_E_pin / E_layers / gcode_commands_per_layer
        printing_z = pin_layer_z

        if self.diving_mode and (not one_shot or current_pin_height != self.pin_height_mm):
            spiral_radius = (self.pin_dimension / 2) - (
                    self.nozzle_outer_diameter / 2) - self.rib_inside_protrusion - self.rib_clearance

            if self.spiral_mode and spiral_radius >= 0.2:  # minimum viable spiral radius (mm)
                angle_step = 360 / gcode_commands_per_layer
            elif not self.spiral_mode:
                angle_step = 0
                spiral_radius = 0
            else:
                raise ValueError(
                    f"The available distance between nozzle and part is too small to have spiral pinning. \n"
                    f"The spiral radius would be {spiral_radius:.2f} mm.")

            # Extrusion tricks init variables
            E_length_geometrical = 0
            blob_E_length = 0
            blob = [0, 0]  # [in_blob_region, is_last_blob_step]

            for step in range(1, int(current_pin_height / step_height) + 1):
                current_z = z + step * step_height
                current_x = x + spiral_radius * math.cos(math.radians(step * angle_step))
                current_y = y + spiral_radius * math.sin(math.radians(step * angle_step))
                adjusted_feedrate = self.pinning_extrusion_speed

                if self.variable_extrusion_enabled and (
                        E_layers - gcode_commands_per_layer) != 0 and not self.geometrical_extrusion_enabled:
                    _denom = int(current_pin_height / step_height - 1)
                    skew_factor = (
                            (self.extrusion_skew_percentage / 100.0) - ((self.extrusion_skew_percentage / 100.0 - 1) *
                                                                        (2 * (step - 1) / _denom if _denom != 0 else 0)))
                    gcode_unskewed_extrusion_length = tot_E_pin / (E_layers * gcode_commands_per_layer)
                    gcode_command_extrusion_length = gcode_unskewed_extrusion_length * (skew_factor)

                if self.geometrical_extrusion_enabled:
                    gcode_command_extrusion_length, blob, deslope = self._extrusion_length_per_step_blob_info(
                        step_height,
                        current_z,
                        pin_layer_z,
                        current_pin_height,
                        pin_structure)

                    # The skewing is not applied to the incomplete pins
                    if self.variable_extrusion_enabled and current_pin_height == self.pin_height_mm:
                        _denom = int(current_pin_height / step_height - 1)
                        skew_factor = (
                                (self.extrusion_skew_percentage / 100.0) - ((self.extrusion_skew_percentage / 100.0 - 1) *
                                                                            (2 * (step - 1) / _denom if _denom != 0 else 0)))
                        gcode_command_extrusion_length = gcode_command_extrusion_length * (skew_factor)
                        if smooth_depressurizing and self.pressure_E_length and deslope[0]:
                            deslope_layers = (pin_structure[1][1] + pin_structure[2][1] + pin_structure[0][
                                1]) / step_height
                            gcode_command_extrusion_length -= self.pressure_E_length / deslope_layers

                    E_length_geometrical += gcode_command_extrusion_length

                printing_z = current_z

                gcode_lines.append(
                    f"G1 X{current_x:.2f} Y{current_y:.2f} Z{printing_z:.2f} E{gcode_command_extrusion_length:.4f} "
                    f"F{adjusted_feedrate:.2f} ; extruding")

                # Check if gcode_command_extrusion_length is negative
                if self.no_pin_retraction and gcode_command_extrusion_length < 0:
                    gcode_lines.pop()  # Remove the last added line
                    remaining_extrusion = gcode_command_extrusion_length

                    # Adjust the previous lines
                    while remaining_extrusion < 0 and gcode_lines:
                        last_line = gcode_lines.pop()
                        if "E" in last_line:
                            parts = last_line.split(" ")
                            for i, part in enumerate(parts):
                                if part.startswith("E"):
                                    previous_extrusion = float(part[1:])
                                    new_extrusion = previous_extrusion + remaining_extrusion
                                    if new_extrusion > 0:
                                        parts[i] = f"E{new_extrusion:.4f}"
                                        gcode_lines.append(" ".join(parts))
                                        remaining_extrusion = 0
                                    else:
                                        remaining_extrusion = new_extrusion
                                    break

                if self.cone_blob and blob[0]:
                    blob_E_length += gcode_command_extrusion_length
                    gcode_lines.pop()
                    if blob[1]:
                        gcode_lines.append(
                            f"G1 X{current_x:.2f} Y{current_y:.2f} Z{printing_z:.2f} "
                            f"E{blob_E_length:.4f} F{self.blob_feedrate:.2f} ; cone blob")

            # Check if the total extrusion length is within 5% of tot_E_pin.
            # Only applied to full-height pins: partial pins (staggered start layers) use an
            # adjusted geometry that is not comparable to the standard rivet volume formula.
            if self.geometrical_extrusion_enabled and current_pin_height == self.pin_height_mm:
                if smooth_depressurizing:
                    E_length_geometrical += self.pressure_E_length
                if not (0.95 * tot_E_pin <= E_length_geometrical <= 1.15 * tot_E_pin):  # tolerance band: −5 % / +15 %
                    raise ValueError(
                        f"Total extrusion length {E_length_geometrical:.4f} is not within 5% of expected {tot_E_pin:.4f}")

        elif self.diving_mode and one_shot:
            pre_retraction = 0.0
            if current_pin_height == self.pin_height_mm:
                z_cone_elevation = self.pin_height_mm / 4 + 0.1
                z_cylinder_elevation = pin_structure[1][1] / 2
            else:
                z_cone_elevation = pin_structure[0][1]
                z_cylinder_elevation = 0
            z += z_cone_elevation
            gcode_lines.append(
                f"G1 Z{z:.2f} E{tot_E_pin + self.pressure_E_length + pre_retraction:.4f} "
                f"F{self.pressure_E_speed:.2f} ; one-shot cone")
            if z_cylinder_elevation > 0:
                z += z_cylinder_elevation
                gcode_lines.append(
                    f"G1 Z{z:.2f} E-{self.pressure_E_length:.4f} F{self.pinning_extrusion_speed * 0.8} ; de-pressurizing"  # reduce speed 20 % during de-pressurizing
                )

        else:
            gcode_lines.append(f"G1 E{tot_E_pin:.4f} F{self.pinning_extrusion_speed}  ; extruding")

        if self.pressure_E_length and not smooth_depressurizing and not one_shot:
            gcode_lines.append(
                f"G1 Z{printing_z:.2f} E-{self.pressure_E_length:.4f} F{self.pressure_E_speed} ; de-pressurizing")

        if self.nozzle_extrude_sunk:
            gcode_lines.append(
                f"G1 Z{pin_layer_z:.3f} E{0:.4f} F{self.pinning_extrusion_speed} ; LIFTING NOZZLE from sunk")

        if one_shot:
            WAIT_MS = 2000  # 2-second dwell after one-shot pin
            gcode_lines.append(f"G4 P{WAIT_MS} ; WAIT")

        if self.wipe_enabled:
            gcode_lines.append(f"; SPIRAL WIPING")
            gcode_lines.extend(self._generate_spiraling_wipe_gcode(x, y, (self.pin_dimension / 2 - 0.1), 3,
                                                                   12, self.wipe_speed))

            gcode_lines.extend(self._generate_spiraling_wipe_gcode(x, y, (self.pin_dimension / 2 + 0.5), 8,
                                                                   12, self.wipe_speed, reverse=True,
                                                                   stop_radius=(self.pin_dimension / 2 - 0.1)))

            # ONLY WORKS IF SPECIMENS ORIENTED IN A SPECIFIC WAY
            gcode_lines.append(f"; SIDE WIPING")
            gcode_lines.extend(
                self._generate_serpentine_wipe_gcode(x, y, pin_layer_z, self.pin_dimension + 0.5,
                                                     self.smallest_side + self.nozzle_outer_diameter * 0.6, 10,
                                                     self.wipe_speed))

        gcode_lines.extend([
            f"; End pin {idx + 1} at layer {layer}",
            ""
        ])

        if round(pin_layer_z, 4) == round(self.pin_height_mm, 4):
            # Determine the output file path
            script_dir = Path(__file__).parent
            output_dir = script_dir / "pin_extrusion_review"
            output_file = output_dir / "gcode_snippets.csv"

            # Export the pin G-code to CSV
            output_dir.mkdir(parents=True, exist_ok=True)
            export_pin_gcode_to_csv(gcode_lines, output_file)

        return gcode_lines

    def _generate_spiraling_wipe_gcode(self, x, y, spiral_radius, num_turns, points_per_turn, travel_speed,
                                       reverse: bool = False,
                                       stop_radius=None):
        """
        Generate G-code for a spiral wipe pattern centred on (x, y).

        Args:
            x, y (float): Centre of the spiral (mm).
            spiral_radius (float): Outer radius of the spiral (mm).
            num_turns (int): Number of full turns.
            points_per_turn (int): Line segments per full turn (higher = smoother).
            travel_speed (float): Feedrate for wipe moves (mm/min).
            reverse (bool): If True, iterate from outer to inner radius. Default False.
            stop_radius (float | None): When reverse=True, stop when radius drops below
                this value (mm). Ignored when reverse=False.

        Returns:
            list[str]: G-code lines.
        """

        wipe_gcode = []
        total_points = num_turns * points_per_turn  # Total number of points to generate

        indices = range(total_points - 1, -1, -1) if reverse else range(total_points)
        for i in indices:
            angle = i * (360 / points_per_turn)  # Angle for each point
            current_radius = spiral_radius * (i / total_points) if not reverse else spiral_radius * ((total_points - 1 - i) / total_points)  # Incrementally increase the radius
            if stop_radius and current_radius < stop_radius:
                break
            x_offset = x + current_radius * math.cos(math.radians(angle))
            y_offset = y + current_radius * math.sin(math.radians(angle))
            wipe_gcode.append(
                f"G1 X{x_offset:.3f} Y{y_offset:.3f} F{travel_speed} ; spiral wipe"
            )

        # Add an extra full circle at the end
        for i in range(points_per_turn):
            angle = i * (360 / points_per_turn)  # Angle for each point
            x_offset = x + spiral_radius * math.cos(math.radians(angle))
            y_offset = y + spiral_radius * math.sin(math.radians(angle))
            wipe_gcode.append(
                f"G1 X{x_offset:.3f} Y{y_offset:.3f} F{travel_speed} ; extra full circle"
            )

        return wipe_gcode

    def _generate_serpentine_wipe_gcode(self, x, y, z, width, height, num_passes, travel_speed):
        """
        Generate G-code for a serpentine wipe pattern.

        Args:
            x (float): X-coordinate of the center of the hole.
            y (float): Y-coordinate of the center of the hole.
            width (float): Width of the specimen (longer direction).
            height (float): Height of the specimen (narrower direction).
            num_passes (int): Number of passes for the serpentine pattern.
            travel_speed (float): Travel speed for the wipe.

        Returns:
            list: List of G-code lines for the serpentine wipe pattern.
        """
        wipe_gcode = []
        step_height = (height / num_passes) / 2  # Height of each serpentine pass

        wipe_gcode.extend(
            self._generate_half_serpentine(x, y, z, width, step_height, num_passes, travel_speed, direction=1))
        wipe_gcode.extend(
            self._generate_half_serpentine(x, y, z, width, step_height, num_passes, travel_speed, direction=-1))

        return wipe_gcode

    def _generate_half_serpentine(self, x, y, z, width, step_height, num_passes, travel_speed, direction):
        """
        Generate G-code for half of the serpentine wipe pattern.

        Args:
            x (float): X-coordinate of the center of the hole.
            y (float): Y-coordinate of the center of the hole.
            width (float): Width of the specimen (longer direction).
            step_height (float): Height of each serpentine pass.
            num_passes (int): Number of passes for the serpentine pattern.
            travel_speed (float): Travel speed for the wipe.
            direction (int): Direction of the serpentine pattern (1 for first half, -1 for second half).

        Returns:
            list: List of G-code lines for half of the serpentine wipe pattern.
        """
        half_wipe_gcode = []

        y_pos = y
        x_pos = x

        for i in range(num_passes):
            if i % 2 == 0:
                factor = 1 * direction
            else:
                factor = -1 * direction

            if self.parts_dict[0]['rotation'] == 0:
                x_pos += (width / 2) * factor
                half_wipe_gcode.append(
                    f"G1 X{x_pos:.3f} Y{y_pos:.3f} F{travel_speed} ; large step")
                y_pos += (step_height * direction)
                half_wipe_gcode.append(
                    f"G1 X{x_pos:.3f} Y{y_pos:.3f} F{travel_speed} ; small step")
                x_pos -= (width / 2) * factor

            elif self.parts_dict[0]['rotation'] == 90:
                y_pos += (width / 2) * factor
                half_wipe_gcode.append(
                    f"G1 X{x_pos:.3f} Y{y_pos:.3f} F{travel_speed} ; large step")
                x_pos += (step_height * direction)
                half_wipe_gcode.append(
                    f"G1 X{x_pos:.3f} Y{y_pos:.3f} F{travel_speed} ; small step")
                y_pos -= (width / 2) * factor
            else:
                print(f"Warning: serpentine wipe skipped — unsupported part rotation ({self.parts_dict[0]['rotation']}°).")

        return half_wipe_gcode

    def _rivet_like_pin_extrusion_length(self, pin_height):
        assert self.pin_rivet_parameters is not None
        smaller_radius = self.pin_rivet_parameters["cylinder_radius"]
        larger_radius = self.pin_rivet_parameters["cone_radius"]
        cylinder_height = self.pin_rivet_parameters["cylinder_height"] * 2
        cone_height = self.pin_rivet_parameters["cone_height"]
        pin_height = round(pin_height, 5)

        # Calculate the volume of one truncated cone
        cone_volume = self._calculate_truncated_cone_volume(smaller_radius, larger_radius, cone_height)

        pin_volume = 0.0
        if pin_height <= cone_height:
            # Only the conical part
            adjusted_cone_volume = self._calculate_truncated_cone_volume(smaller_radius,
                                                                         smaller_radius + (
                                                                                 larger_radius - smaller_radius) * (
                                                                                 pin_height / cone_height),
                                                                         pin_height)
            pin_volume = adjusted_cone_volume
        elif (cone_height + cylinder_height) >= pin_height > cone_height:
            # One full cone and part of the cylinder
            adjusted_cylinder_volume = math.pi * (smaller_radius ** 2) * (pin_height - cone_height)
            pin_volume = cone_volume + adjusted_cylinder_volume
        elif (cone_height + cylinder_height) < pin_height <= (2 * cone_height + cylinder_height):
            # One full cone, full cylinder, and part of the second cone
            adjusted_cone_height = pin_height - (cone_height + cylinder_height)
            adjusted_cone_volume = self._calculate_truncated_cone_volume(smaller_radius,
                                                                         smaller_radius + (
                                                                                 larger_radius - smaller_radius) * (
                                                                                 (pin_height - (
                                                                                         cone_height + cylinder_height))
                                                                                 / cone_height), adjusted_cone_height)
            cylinder_volume = math.pi * (smaller_radius ** 2) * cylinder_height
            pin_volume = cone_volume + cylinder_volume + adjusted_cone_volume
        else:
            raise ValueError(
                f"Pin height {pin_height:.3f} mm exceeds the maximum rivet geometry height "
                f"({2 * cone_height + cylinder_height:.3f} mm). Check pin_rivet_parameters.")

        # Filament cross-sectional area: π * (filament_radius)^2
        filament_radius = self.FILAMENT_DIAMETER / 2
        filament_cross_section = math.pi * (filament_radius ** 2)

        # Extrusion length = pin volume / filament cross-sectional area, adjusted by the flow ratio
        extrusion_length = (pin_volume / filament_cross_section) * self.flow_ratio

        return extrusion_length

    def _calculate_truncated_cone_volume(self, smaller_radius, larger_radius, height):
        return (1 / 3) * math.pi * height * (smaller_radius ** 2 + smaller_radius * larger_radius + larger_radius ** 2)

    def _extrusion_length_per_step_blob_info(self, step_height, current_z, pin_layer_z, pin_height, pin_structure):
        """
        Compute extrusion length and blob/deslope flags for a single step within a rivet-shaped pin.

        Returns:
            tuple:
                extrusion_length (float): Filament length to extrude for this step (mm).
                blob (list[int, int]): blob[0]=1 when inside the cone-blob accumulation region;
                    blob[1]=1 on the last step of that region (triggers blob flush).
                deslope (list[int, int]): deslope[0]=1 when pressure de-slope should apply
                    (used when smooth_depressurizing is active).
        """
        assert self.pin_rivet_parameters is not None
        smaller_radius = self.pin_rivet_parameters["cylinder_radius"]
        larger_radius = self.pin_rivet_parameters["cone_radius"]
        cone_height = self.pin_rivet_parameters["cone_height"]

        slope = (larger_radius - smaller_radius) / cone_height

        relative_z = round(current_z - pin_layer_z + pin_height + self.nozzle_sinking, 3)

        # Determine the section based on relative_z and pin_structure
        current_section = None
        cumulative_height = 0
        blob = [0, 0]
        deslope = [0, 0]

        for section, height in pin_structure:
            cumulative_height += height
            if relative_z <= cumulative_height:
                current_section = section
                break

        if current_section is None:
            raise ValueError(
                f"Relative Z {relative_z:.3f} mm exceeds total pin height {pin_height:.3f} mm — "
                f"step is outside pin bounds. Check pin structure decomposition.")

        if current_section == "lower_cone":
            shift = cone_height - pin_structure[0][1]
            lower_radius = larger_radius - slope * (relative_z + shift - step_height)
            upper_radius = larger_radius - slope * (relative_z + shift)

            blob[0] = 1
            deslope[0] = 1
            if relative_z == cumulative_height:
                blob[1] = 1

        elif current_section == "cylinder":
            lower_radius = smaller_radius
            upper_radius = smaller_radius
            deslope[0] = 1

        else:  # upper_cone
            shift = relative_z - pin_structure[0][1] - pin_structure[1][1]
            lower_radius = smaller_radius + slope * (shift - step_height)
            upper_radius = smaller_radius + slope * (shift)
            deslope[0] = 1

        # Calculate the average radius for the step
        average_radius = (lower_radius + upper_radius) / 2

        # Calculate the volume of the step
        step_volume = math.pi * (average_radius ** 2) * step_height

        # Filament cross-sectional area: π * (filament_radius)^2
        filament_radius = self.FILAMENT_DIAMETER / 2
        filament_cross_section = math.pi * (filament_radius ** 2)

        # Extrusion length = step volume / filament cross-sectional area, adjusted by the flow ratio
        extrusion_length = (step_volume / filament_cross_section) * self.flow_ratio

        return extrusion_length, blob, deslope


def export_pin_gcode_to_csv(gcode_lines, output_file):
    """
    Export the G-code lines related to pin extrusion to a CSV file.

    Args:
        gcode_lines (list): List of G-code lines.
        output_file (str): Path to the output CSV file.
    """
    start_index = None
    end_index = None

    # Find the start and end indices of the pin extrusion section
    for i, line in enumerate(gcode_lines):
        if "; EXTRUDING PIN" in line:
            start_index = i + 1
        elif start_index is not None and "WIPING" in line:
            end_index = i
            break

    if start_index is None:
        print("Warning: pin extrusion section not found in G-code lines — CSV export skipped.")
        return

    if end_index is None:
        end_index = len(gcode_lines)

    # Extract the relevant G-code lines
    pin_gcode_lines = gcode_lines[start_index:end_index]

    # Write the extracted data to a CSV file
    with open(output_file, mode='w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(["Z", "E", "F"])  # Write the header

        for line in pin_gcode_lines:
            if line.startswith("G1"):
                parts = line.split()
                z_value = None
                e_value = None
                f_value = None

                for part in parts:
                    if part.startswith("Z"):
                        z_value = part[1:]
                    elif part.startswith("E"):
                        e_value = part[1:]
                    elif part.startswith("F"):
                        f_value = part[1:]

                if z_value and e_value and f_value:
                    writer.writerow([z_value, e_value, f_value])
