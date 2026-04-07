"""
pin_layout.py
-------------
Defines and validates the layout of reinforcing pins within the cross-section of an FDM specimen.

Given the specimen cross-section dimensions, pin geometry, and centre-to-centre pin spacing,
PinDefinition computes the XY position of each pin centre — always symmetric about the
cross-section midpoint — and checks that every pin fits inside the boundary.

The resulting infill percentage is printed as informational output (not a constraint).

The resulting pin layout is consumed by ``GCodeCommandsComposer`` in ``pin_gcode_generator.py``.
"""
from math import pi

class PinDefinition:
    """
    Computes and validates pin positions inside a rectangular specimen cross-section.

    Pins are placed on a regular grid, centred symmetrically about the midpoint of the
    cross-section.  The spacing between adjacent pin centres is specified directly via
    ``pin_center_distance_x`` and ``pin_center_distance_y``.

    Args:
        largest_side (float): Longer cross-section dimension (mm), typically the specimen width.
        smallest_side (float): Shorter cross-section dimension (mm), typically the specimen thickness.
        pin_dimension (float): Pin diameter in mm.
        num_pins_largest_side (int): Number of pins along the larger dimension.
        num_pins_smallest_side (int): Number of pins along the smaller dimension.
        pin_height_input (float): Pin height expressed in ``pin_height_input_type`` units.
        pin_height_input_type (str): ``'layers'`` or ``'mm'``.
        layer_height (float): Slicer layer height in mm.
        pin_center_distance_x (float): Centre-to-centre spacing between adjacent pins along the
            larger cross-section axis (mm).
        pin_center_distance_y (float): Centre-to-centre spacing between adjacent pins along the
            smaller cross-section axis (mm).

    Usage::

        pin_def = PinDefinition(...)
        data = pin_def.define_pins_relative_xy()  # validates and returns layout dict
        pin_def.visualize_pin_layout()             # optional: render cross-section preview
    """
    def __init__(self, largest_side, smallest_side, pin_dimension, num_pins_largest_side,
                 num_pins_smallest_side, pin_height_input, pin_height_input_type, layer_height,
                 pin_center_distance_x, pin_center_distance_y):

        # cross-section parameters
        self.largest_side = largest_side
        self.smallest_side = smallest_side

        # pin dimensions and layout parameters
        self.pin_dimension = pin_dimension
        self.num_pins_largest_side = num_pins_largest_side
        self.num_pins_smallest_side = num_pins_smallest_side
        self.pin_height_input = pin_height_input
        self.pin_height_input_type = pin_height_input_type

        # printing parameters
        self.layer_height = layer_height
        self.pin_center_distance_x = pin_center_distance_x
        self.pin_center_distance_y = pin_center_distance_y

        # Populated by define_pins_relative_xy(); consumed by visualize_pin_layout(). None until then.
        self.pins_relative_xy = None

    def calculate_pin_height(self) -> float:
        """
        Return the pin height in millimetres.

        Converts ``pin_height_input`` from its native unit to mm:
          - ``'layers'``: multiplies by ``layer_height``.
          - ``'mm'``:     returns ``pin_height_input`` unchanged.
        """
        if self.pin_height_input_type == "layers":
            return self.pin_height_input * self.layer_height
        elif self.pin_height_input_type == "mm":
            return self.pin_height_input
        else:
            raise ValueError("Problem: pin_height_input_type must be either 'layers' or 'mm'.")

    def _fit_in_cross_section(self) -> bool:
        """
        Return True if every pin fits entirely within the specimen boundary.

        Checks that no pin edge (centre ± pin_dimension/2) falls outside the cross-section
        rectangle on any side.  Returns False if ``pins_relative_xy`` has not been set.
        """
        if self.pins_relative_xy is None:
            return False
        half_d = self.pin_dimension / 2
        for (x, y) in self.pins_relative_xy:
            if (x - half_d < 0 or x + half_d > self.largest_side or
                    y - half_d < 0 or y + half_d > self.smallest_side):
                return False
        return True

    def _calculate_pin_positions(self) -> list:
        """
        Compute the XY position of each pin centre in the specimen's local coordinate frame.

        The origin (0, 0) is at the bottom-left corner of the cross-section rectangle.
        Pins are arranged on a uniform grid centred on the cross-section midpoint.
        Adjacent pin centres are separated by ``pin_center_distance_x`` (along the larger
        axis) and ``pin_center_distance_y`` (along the smaller axis).

        For N pins along an axis the positions are:
            centre_of_section + (i - (N-1)/2) * pin_center_distance   for i = 0 … N-1

        Returns:
            list[tuple[float, float]]: Pin centres as (x, y) pairs rounded to 4 decimal places,
                in row-major order (largest-side index i varies in the outer loop).
        """
        cx = self.largest_side / 2
        cy = self.smallest_side / 2

        pin_positions = []
        for i in range(self.num_pins_largest_side):
            x = cx + (i - (self.num_pins_largest_side - 1) / 2) * self.pin_center_distance_x
            for j in range(self.num_pins_smallest_side):
                y = cy + (j - (self.num_pins_smallest_side - 1) / 2) * self.pin_center_distance_y
                pin_positions.append((round(x, 4), round(y, 4)))

        return pin_positions

    def visualize_pin_layout(self):
        """
        Render a matplotlib figure showing pin positions within the specimen cross-section.

        Raises:
            RuntimeError: If ``define_pins_relative_xy()`` has not been called yet.

        Note:
            matplotlib is imported lazily inside this method so it remains an
            optional dependency at module load time.
        """
        # Imported here so matplotlib remains optional at module load time.
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches

        if self.pins_relative_xy is None:
            raise RuntimeError("Call define_pins_relative_xy() before visualize_pin_layout().")

        _, ax = plt.subplots()
        ax.add_patch(
            mpatches.Rectangle((0, 0), self.largest_side, self.smallest_side, edgecolor='black', facecolor='none', lw=2))

        for (x, y) in self.pins_relative_xy:
            ax.add_patch(mpatches.Circle((x, y), self.pin_dimension / 2, edgecolor='blue', facecolor='blue', alpha=0.5))

        ax.set_xlim(0, self.largest_side)
        ax.set_ylim(0, self.smallest_side)
        ax.set_aspect('equal', 'box')
        plt.xlabel('x (mm)')
        plt.ylabel('y (mm)')
        plt.title('Pin layout')
        plt.show()

    def define_pins_relative_xy(self) -> dict:
        """
        Place pins and validate that they fit within the specimen cross-section.

        Prints the resulting pin infill percentage as informational output.

        Returns:
            dict: Pin configuration with keys ``largest_side``, ``smallest_side``,
                ``pins_relative_xy``, ``pin_height_mm``, ``pin_dimension``,
                ``layer_height``.

        Raises:
            ValueError: If any pin edge falls outside the cross-section boundary.

        Side effect:
            Sets ``self.pins_relative_xy`` to the computed positions.
            ``visualize_pin_layout()`` requires this method to be called first.
        """
        self.pins_relative_xy = self._calculate_pin_positions()

        # Report resulting infill as information (not a constraint).
        total_pin_area = (self.num_pins_largest_side * self.num_pins_smallest_side
                          * pi * (self.pin_dimension / 2) ** 2)
        infill_pct = total_pin_area / (self.largest_side * self.smallest_side) * 100
        print(f"\n[Step 1] Pin layout")
        print(f"  Pins: {self.num_pins_largest_side} x {self.num_pins_smallest_side}"
              f"  |  Spacing: {self.pin_center_distance_x} mm (X) x {self.pin_center_distance_y} mm (Y)"
              f"  |  Infill: {infill_pct:.1f} %")

        if self._fit_in_cross_section():
            print(f"  All pin edges within {self.largest_side} x {self.smallest_side} mm boundary. Layout OK.")
            return {
                "largest_side": self.largest_side,
                "smallest_side": self.smallest_side,
                "pins_relative_xy": self.pins_relative_xy,
                "pin_height_mm": self.calculate_pin_height(),
                "pin_dimension": self.pin_dimension,
                "layer_height": self.layer_height,
            }
        else:
            self.pins_relative_xy = None
            raise ValueError("Problem: pins do not fit within the cross-section. "
                             "Reduce pin_center_distance, pin_dimension, or number of pins.")
