"""
template.py  —  FDM-Pinning configuration template
----------------------------------------------------
Copy this file and rename it to match your G-code file (e.g. ``my_specimen.py``).
Adjust the parameters below, then run from the repo root:

    python -m configs.my_specimen

The script looks for a G-code file whose name contains the config file stem
(e.g. ``gcode_input/*my_specimen*.gcode``) and writes the post-processed result to
``gcode_output/``.

Parameter guide
~~~~~~~~~~~~~~~
Every parameter is documented inline.  Units are always noted in the comment.
Start with the SPECIMEN GEOMETRY and PINNING CONFIGURATION blocks — those are the
ones you will change most often.  The CONSTANT PARAMETERS block rarely needs editing
once dialled in for a particular printer.
"""

import sys
from pathlib import Path

# Ensure the repo root is on sys.path so the core modules can be imported
# regardless of how this script is invoked.
sys.path.insert(0, str(Path(__file__).parent.parent))

from pin_layout import PinDefinition
from pin_gcode_generator import GCodeCommandsComposer
from gcode_injector import GCodeModifier


# ──────────────────────────────────────────────────────────────────────────────
# SPECIMEN GEOMETRY
# ──────────────────────────────────────────────────────────────────────────────

# Height of the gauge section that will receive pins (mm).
# This defines how many total layers the composer iterates over.
specimen_height_mm = 10.0  # mm

# Layer height used in the slicer (mm).  Must match the slicer setting exactly.
layer_height = 0.05  # mm

# Cross-section dimensions of the specimen (mm).
specimen_largest_side  = 10.0  # mm — longer cross-section dimension
specimen_smallest_side = 4.0   # mm — shorter cross-section dimension


# ──────────────────────────────────────────────────────────────────────────────
# PINNING CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

# Diameter of each pin (mm).
# For pins with a variable diameter profile (rivet shape), set this to the
# *largest* diameter across the height profile (i.e. the cone base radius × 2).
pin_diameter = 2.0  # mm

# Number of pins distributed along each cross-section axis.
num_pins_largest_side  = 3  # along the longer dimension
num_pins_smallest_side = 1  # along the shorter dimension

# Height of each pin and the unit in which it is expressed.
pin_height_layers     = 28         # layers  (or mm if pin_height_input_type = "mm")
pin_height_input_type = "layers"   # "layers" or "mm"

# Centre-to-centre distance between adjacent pins (mm).
# Pins are placed symmetrically about the cross-section midpoint.
# Example: 3 pins at 2.6 mm spacing on a 10 mm wide specimen →
#   centres at 2.4, 5.0, 7.6 mm (2.4 mm from each edge to the nearest pin centre).
# The tool prints the resulting infill percentage at runtime for reference.
pin_center_distance_x = 3  # mm — along the larger cross-section axis
pin_center_distance_y = 2  # mm — along the smaller cross-section axis

# Stagger parameters — controls which layer each pin *starts* at so that
# adjacent pins are offset in height, improving mechanical interlocking.
# "fixed_start_layers": list of start layers, one per pin index (wraps around).
# Set stagger_params = None for all pins to start at the same layer.
stagger_params = {
    "fixed_start_layers": [28, 10, 20],  # example for 3 pins: pin 1 starts at layer 28, pin 2 at layer 9, pin 3 at layer 18
}

# Rivet geometry — shapes the pin as: lower cone → cylinder → upper cone.
# "cone_radius"    (mm): radius of the cone base (= largest pin radius).
# "cone_height"    (mm): axial height of each cone section.
# "cylinder_radius"(mm): radius of the cylindrical mid-section.
# "cylinder_height"(mm): half-height of the cylinder (doubled internally).
# Set pin_rivet_parameters = None to extrude a plain cylinder.
pin_rivet_parameters = {
    "cone_radius":     1.0,  # mm
    "cone_height":     0.4,  # mm
    "cylinder_radius": 0.7,  # mm
    "cylinder_height": 0.3,  # mm
}


# ──────────────────────────────────────────────────────────────────────────────
# PART POSITIONS ON THE BUILD PLATE
# ──────────────────────────────────────────────────────────────────────────────
# Each dict defines one part on the build plate.  Duplicate the block for
# multi-part prints.
#   "name"     — arbitrary label (used for G-code comments only)
#   "xy"       — (X, Y) position of the part origin on the build plate (mm)
#   "rotation" — rotation of the part around its origin (degrees)

x_shift = 20.0  # mm — build-plate X origin of the first part
y_shift = 20.0  # mm — build-plate Y origin of the first part

parts_on_build_plate = [
    {
        "name": "part_1",
        "xy": (x_shift, y_shift),
        "rotation": 0.0,  # degrees
    },
    # Add more parts here if your G-code contains multiple copies:
    # {
    #     "name": "part_2",
    #     "xy": (x_shift + 15.0, y_shift),
    #     "rotation": 90.0,
    # },
]


# ──────────────────────────────────────────────────────────────────────────────
# EXTRUSION PARAMETERS
# ──────────────────────────────────────────────────────────────────────────────

# Extrusion multiplier (dimensionless).  1.0 = theoretical volume;
# values > 1 produce over-extrusion, which is needed to fill the cavity walls
# and achieve mechanical interlocking.
flow_ratio = 1.2  # dimensionless

# Feedrate during pin extrusion (mm/min).
pinning_extrusion_speed = 30 * 60  # = 1800 mm/min

# --- Variable extrusion ---
# Ramps the extrusion rate along the pin height.
# 100 % = uniform; 200 % = extrusion doubles linearly from base to tip.
variable_extrusion_enabled = True
extrusion_skew_percentage  = 200  # %

# --- Geometrical extrusion (rivet shape) ---
# When True, uses pin_rivet_parameters above to shape the pin.
geometrical_extrusion_enabled = True

# Add a small blob of material at the cone tip to ensure full cavity filling.
cone_blob     = True
blob_feedrate = 100 * 60  # mm/min


# ──────────────────────────────────────────────────────────────────────────────
# NOZZLE BEHAVIOUR
# ──────────────────────────────────────────────────────────────────────────────

# diving_mode = True  → nozzle dives into the deposited layers from below.
# diving_mode = False → pin is built up from the current top surface.
diving_mode = True

# Additional depth the nozzle sinks below the layer surface before extruding (mm).
# 0.0 disables sinking.
nozzle_sinking = 0.1  # mm

# Speed of the sinking move (mm/min).
nozzle_sinking_speed = 30.0 * 60  # = 1800 mm/min

# Dwell time at the bottom of the sinking move (seconds).  0.0 = no dwell.
nozzle_sinking_wait_time = 0.0  # s

# Apply sinking on the very first pinning layer as well. Leave disabled to avoid 
# collision if this would damage the nozzle or build plate
nozzle_sinking_1st_layer = True

# Extrude filament while the nozzle is still sunk (pre-fills the pin base). Used
# to build up pressure in the nozzle at the pin base.
nozzle_extrude_sunk = True

# Spiral path during diving (experimental, keep False unless in development). The
# nozzle follows a spiral path as it lifts and extrudes.
spiral_mode = False

# Outer diameter of the nozzle tip — used in spiral/rib geometry calculations (mm).
nozzle_outer_diameter = 1.45  # mm

# Rib geometry (experimental, set both to 0.0 to disable unless in development).
rib_inside_protrusion = 0.0  # mm
rib_clearance         = 0.0  # mm


# ──────────────────────────────────────────────────────────────────────────────
# RETRACTION & TRAVEL
# ──────────────────────────────────────────────────────────────────────────────

retraction_length = 0.8  # mm — filament retraction before travel moves
z_hop_length      = 0.2  # mm — Z-hop height above the surface before travelling

# Skip filament retraction before travelling to the next pin.
# Useful at very low flow rates where retraction causes under-extrusion.
no_pin_retraction = True

# Pre-pressurisation move before pin extrusion begins.
# Pushes a small amount of filament to build nozzle pressure.  0.0 = disabled.
pressure_E_length = 0.0  # mm
pressure_E_speed  = 100 * 60  # mm/min


# ──────────────────────────────────────────────────────────────────────────────
# WIPE
# ──────────────────────────────────────────────────────────────────────────────

# Perform a short wipe move after retraction to reduce stringing.
wipe_enabled = True
wipe_speed   = 60.0 * 60  # mm/min


# ──────────────────────────────────────────────────────────────────────────────
# CONSTANT PARAMETERS  (rarely need changing per print)
# ──────────────────────────────────────────────────────────────────────────────

z_lift_speed    = 25.0 * 60   # mm/min — Z travel speed during hops
xy_travel_speed = 150.0 * 60  # mm/min — XY travel speed between pins
z_drop_speed    = 10.0 * 60   # mm/min — speed to descend to pin entry point

# Set to a temperature (°C) to heat the nozzle specifically for each pin.
# False = use the normal printing temperature throughout.
heated_pin = False


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    # Step 1 — Define pin geometry and validate layout
    pin_def = PinDefinition(
        largest_side=specimen_largest_side,
        smallest_side=specimen_smallest_side,
        pin_dimension=pin_diameter,
        num_pins_largest_side=num_pins_largest_side,
        num_pins_smallest_side=num_pins_smallest_side,
        pin_height_input=pin_height_layers,
        pin_height_input_type=pin_height_input_type,
        layer_height=layer_height,
        pin_center_distance_x=pin_center_distance_x,
        pin_center_distance_y=pin_center_distance_y,
    )

    # Step 2 — Compute absolute pin positions.
    # Uncomment the next line to display a cross-section preview before running.
    pin_cross_section_data = pin_def.define_pins_relative_xy()
    # pin_def.visualize_pin_layout()

    # Step 3 — Compose the per-layer pinning G-code
    snippet_composer = GCodeCommandsComposer(
        pin_cross_section_data,
        parts_on_build_plate,

        specimen_height_mm=specimen_height_mm,
        flow_ratio=flow_ratio,

        z_lift_speed=z_lift_speed,
        xy_travel_speed=xy_travel_speed,
        z_hop_length=z_hop_length,
        retraction_length=retraction_length,
        z_drop_speed=z_drop_speed,
        wipe_speed=wipe_speed,

        pinning_extrusion_speed=pinning_extrusion_speed,

        diving_mode=diving_mode,
        spiral_mode=spiral_mode,
        nozzle_outer_diameter=nozzle_outer_diameter,
        rib_inside_protrusion=rib_inside_protrusion,
        rib_clearance=rib_clearance,
        nozzle_sinking=nozzle_sinking,
        nozzle_sinking_speed=nozzle_sinking_speed,
        nozzle_sinking_wait_time=nozzle_sinking_wait_time,
        nozzle_sinking_1st_layer=nozzle_sinking_1st_layer,
        nozzle_extrude_sunk=nozzle_extrude_sunk,

        wipe_enabled=wipe_enabled,

        variable_extrusion_enabled=variable_extrusion_enabled,
        extrusion_skew_percentage=extrusion_skew_percentage,

        geometrical_extrusion_enabled=geometrical_extrusion_enabled,
        cone_blob=cone_blob,
        blob_feedrate=blob_feedrate,

        no_pin_retraction=no_pin_retraction,
        pressure_E_length=pressure_E_length,
        pressure_E_speed=pressure_E_speed,

        heated_pin=heated_pin,
        stagger_params=stagger_params,
        pin_rivet_parameters=pin_rivet_parameters,
    )

    gcode_lines, constants = snippet_composer.compose_layer_gcode()

    # Step 4 — Merge the pinning G-code into the base slicer G-code and save
    config_stem = Path(__file__).stem
    gcode_dir = Path(__file__).parent.parent / "gcode_input"

    matched = list(gcode_dir.glob(f"*{config_stem}*.gcode"))
    if not matched:
        print(f"Error: no G-code file matching '*{config_stem}*.gcode' found in '{gcode_dir}'.")
        print(f"  Tip: the G-code filename must contain '{config_stem}' to be matched.")
        sys.exit(1)
    if len(matched) > 1:
        print(f"Warning: {len(matched)} files match '*{config_stem}*.gcode' — all will be processed.")

    for gcode_file in matched:
        gcode_modifier = GCodeModifier(gcode_file.stem, constants["layer_height"])
        gcode_modifier.read_gcode_file()
        gcode_modifier.insert_pin_gcode(gcode_lines, constants, start_layer=0)


if __name__ == "__main__":
    main()
