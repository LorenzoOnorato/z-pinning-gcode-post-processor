"""
gcode_injector.py
-----------------
Reads a slicer-generated G-code file, inserts the pinning G-code produced by
GCodeCommandsComposer at the correct layer boundaries, and writes the modified
file to ``gcode_output/``.

Workflow:
  1. ``GCodeModifier.read_gcode_file()``  — load and parse the base G-code.
  2. ``GCodeModifier.insert_pin_gcode()`` — splice in pinning commands layer by layer.
  3. The output file is saved automatically; the filename encodes the active
     pinning parameters (pin height, diving mode, sinking depth, flow ratio, etc.)
     for traceability.

Helper
------
``parse_gcode_lines(gcode_lines, layer_height)`` — converts raw G-code text to the
``list[dict]`` representation shared by this module and ``pin_gcode_generator``.
"""
import re
from pathlib import Path


def parse_gcode_lines(gcode_lines: list, layer_height: float) -> list:
    """
    Parses G-code lines into a structured format.

    Args:
        gcode_lines (list[str]): Raw G-code lines as strings.
        layer_height (float): Layer height in mm, used to derive the layer index from Z.

    Returns:
        list[dict]: Each element has keys ``command`` (str|None), ``params`` (dict),
            ``comment`` (str), and ``layer`` (float).

            'layer' (float): Layer index computed as ``round(Z_mm / layer_height, 2)``.
                This must match the integer layer keys in the dict produced by GCodeCommandsComposer.
    """
    if layer_height <= 0:
        raise ValueError(f"layer_height must be positive, got {layer_height!r}")

    parsed_gcode = []

    # Matches: command word (G/M/T + digits), optional axis parameters (XYZEFIJK…),
    # optional trailing comment. Extend the character class for additional axis letters.
    gcode_pattern = re.compile(r'(?P<command>[GMT]\d+)\s*(?P<params>[-XYZEFIJKR0-9.\s]*)\s*(?P<comment>.*)')
    z_pattern = re.compile(r';Z:(-?\d+\.?\d*)')

    current_layer = None
    previous_z = 0

    for line in gcode_lines:
        # Check for Z-axis changes in G-code commands
        z_match = z_pattern.search(line)
        if z_match:
            previous_z = float(z_match.group(1))

        match = gcode_pattern.match(line)

        if match:
            command = match.group('command')
            params = match.group('params').strip()
            comment = match.group('comment').strip() if match.group('comment') else ''
            param_dict = {}

            if params:
                param_pairs = params.split()
                for pair in param_pairs:
                    key = pair[0]
                    value = pair[1:] if len(pair) > 1 else ''
                    param_dict[key] = value

            parsed_gcode.append({
                'command': command,
                'params': param_dict,
                'comment': comment,
                'layer': round(previous_z / layer_height, 2)
            })
        else:
            parsed_gcode.append({
                'command': None,
                'params': {},
                'comment': line.strip(),
                'layer': round(previous_z / layer_height, 2)
            })

    return parsed_gcode

class GCodeModifier:
    """
    Loads a slicer G-code file, inserts pin-extrusion snippets at layer boundaries,
    and saves the result.

    Args:
        filename (str): Stem of the G-code file to load (without ``.gcode`` extension).
            The file must exist in ``gcode_input/``.
        layer_height (float): Layer height in mm, used to map Z coordinates to layer numbers.

    Call ``read_gcode_file()`` before ``insert_pin_gcode()``; the latter depends on
    ``parsed_gcode_lines`` being populated by the former.
    """
    def __init__(self, filename, layer_height):
        self.filename = filename
        self.gcode_lines = None
        self.parsed_gcode_lines = None
        self.modified_gcode_lines = []
        self.layer_height = layer_height

    def read_gcode_file(self) -> None:
        """
        Reads the G-code file from ``gcode_input/<filename>.gcode``
        and parses it into structured form.

        Raises:
            FileNotFoundError: If the file does not exist at the expected path.
        """
        script_dir = Path(__file__).parent
        gcode_dir = script_dir / "gcode_input"
        gcode_file_path = gcode_dir / f"{self.filename}.gcode"

        if not gcode_file_path.is_file():
            raise FileNotFoundError(f"G-code file not found: {gcode_file_path}")

        with open(gcode_file_path, 'r', encoding='utf-8') as file:
            self.gcode_lines = [line.strip() for line in file]

        self.parsed_gcode_lines = parse_gcode_lines(self.gcode_lines, self.layer_height)
        print(f"  Loaded '{gcode_file_path.name}' ({len(self.gcode_lines)} lines).")

    def insert_pin_gcode(self, pin_gcode_dict: dict, constants: dict, start_layer: int = 0) -> None:
        """
        Inserts the pinning G-code into the original G-code at the specified layers
        and saves the result via ``save_gcode()``.

        The pinning snippet is inserted immediately *before* the ``;LAYER_CHANGE``
        sentinel line, so it executes before the slicer's own layer-transition moves.

        Args:
            pin_gcode_dict (dict): Layer→G-code mapping from ``GCodeCommandsComposer.compose_layer_gcode()``.
            constants (dict): Printing parameters written as a header block in the output file.
            start_layer (int): First layer at which pinning commands are inserted (default 0).
        """

        # Generate the constant info as G-code comments
        constant_comments = []
        constant_comments.append({'comment': "; HEADER PINNING PARAMETERS"})
        for key, value in constants.items():
            constant_comments.append({'comment': f"; {key}: {value}"})

        parsed_gcode = self.parsed_gcode_lines
        modified_gcode = []
        header_pin_inserted = False  # Track if pinning header has been inserted
        last_layer_pinned = False  # Track if the last layer was pinned
        last_inserted_layer = None  # Track the most recent layer where pinning was inserted

        for line in parsed_gcode:
            # Insert constants block just before ; thumbnail begin
            if not header_pin_inserted and "; thumbnail begin" in line.get('comment', ''):
                modified_gcode.append({'comment': ""})  # Add a blank line before the constants block
                modified_gcode.extend(constant_comments)
                modified_gcode.append({'comment': ""})  # Add a blank line after the constants block
                header_pin_inserted = True  # Ensure we only insert the header once

            # ACTUAL PINNING GCODE
            if line.get('comment', '') == ";LAYER_CHANGE":
                if line['layer'] in pin_gcode_dict and line['layer'] >= start_layer:
                    modified_gcode.append({'comment': ""})  # Add a blank line
                    modified_gcode.append({'comment': ""})  # Add a blank line
                    for pin_line in pin_gcode_dict[line['layer']]:
                        modified_gcode.append(pin_line)  # Directly append the pin_line (which is already a dictionary)
                    last_inserted_layer = line['layer']
            elif "end_gcode" in line.get('comment', '') and not last_layer_pinned:
                if last_inserted_layer is not None:
                    modified_gcode.append({'comment': ""})  # Add a blank line
                    modified_gcode.append({'comment': ""})  # Add a blank line
                    for pin_line in pin_gcode_dict[last_inserted_layer]:
                        modified_gcode.append(pin_line)  # Directly append the pin_line (which is already a dictionary)
                    last_layer_pinned = True


            modified_gcode.append(line)

        if not header_pin_inserted:
            modified_gcode = [{'comment': ""}, *constant_comments, {'comment': ""}, *modified_gcode]

        layers_modified = sum(1 for l in pin_gcode_dict if l >= start_layer)
        print(f"  Pinning blocks inserted at {layers_modified} layer(s).")

        # Convert modified_gcode (list of dictionaries) to string format
        self.modified_gcode_lines = [self._convert_dict_to_gcode(gcode_dict) for gcode_dict in modified_gcode]

        self.save_gcode(constants)

    def _convert_dict_to_gcode(self, gcode_dict):
        """
        Converts a G-code dictionary back into a string format for saving.

        Args:
            gcode_dict (dict): Dictionary containing 'command', 'params', and 'comment'.

        Returns:
            str: The corresponding G-code line in string format.
        """
        if not gcode_dict.get('command') and not gcode_dict.get('comment'):
            return ""  # blank line sentinel

        gcode_line = ""

        # If there's a command, add it
        if gcode_dict.get('command'):
            gcode_line += gcode_dict['command']
            # If there are parameters, format them as 'key=value' pairs
            if gcode_dict.get('params'):
                for param, value in gcode_dict['params'].items():
                    gcode_line += f" {param}{value}"

        # If there's a comment, append it after the command/params
        if gcode_dict.get('comment'):
            if gcode_line:
                gcode_line += " "  # Add a space before the comment if there's already a command/params
            gcode_line += gcode_dict['comment']

        return gcode_line

    def save_gcode(self, constants):
        """
        Saves the modified G-code to a file.

        Side effect:
            If ``pin_extrusion_review/gcode_snippets.csv`` exists it is renamed to match the
            output G-code filename for traceability.
        """
        script_dir = Path(__file__).parent
        output_dir = script_dir / "gcode_output"
        output_dir.mkdir(exist_ok=True)
        filename_suffix = ""

        filename_suffix += "_phl" + str(constants["pin_height_layers"])
        if constants["diving_mode"] is True:
            filename_suffix += "_dv"
        else:
            filename_suffix += "_top"
        if constants["heated_pin"] is not False and constants["heated_pin"] is not None:
            filename_suffix += f"_hp{constants['heated_pin']}"
        if constants["nozzle_sinking"] != 0:
            filename_suffix += f"_sk{constants['nozzle_sinking']}"
        if constants["nozzle_sinking_wait_time"] != 0:
            filename_suffix += f"_skw{constants['nozzle_sinking_wait_time']}"
        if constants["spiral_mode"]:
            filename_suffix += "_spT"
        if constants["rib_inside_protrusion"] != 0:
            filename_suffix += f"_rb{constants['rib_inside_protrusion']}"
        if constants["rib_clearance"] != 0:
            filename_suffix += f"_rbc{constants['rib_clearance']}"
        if constants["variable_extrusion_enabled"] and constants["extrusion_skew_percentage"] != 0:
            filename_suffix += f"_ve{constants['extrusion_skew_percentage']}"
        if constants["wipe_enabled"]:
            filename_suffix += "_wp"

        filename_suffix += "_" + str(round(constants["flow_ratio"], 3))

        output_file_path = output_dir / f"{self.filename.replace('_CL', '')}{filename_suffix}.gcode"

        with open(output_file_path, 'w', encoding='utf-8') as file:
            for line in self.modified_gcode_lines:
                file.write(f"{line}\n")

        print(f"\n[Step 3] Injection & save")
        print(f"  Output: {output_file_path.name}")
        print(f"  Saved to: {output_file_path.parent}")

        # Rename the gcode_snippets.csv file to match the output G-code filename
        csv_file_path = (script_dir / "pin_extrusion_review")
        csv_old_file_path = csv_file_path / "gcode_snippets.csv"
        new_csv_file_path = csv_file_path / f"{self.filename.replace('_CL', '')}{filename_suffix}.csv"

        if csv_old_file_path.exists():
            if new_csv_file_path.exists():
                new_csv_file_path.unlink()
            csv_old_file_path.rename(new_csv_file_path)
            print(f"  Review: {new_csv_file_path.name}")
        else:
            print(f"  Review: pin extrusion CSV not found — skipping.")
