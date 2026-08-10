"""
Entry point: `python scripts/split_project_info_by_year.py <source.xlsx> <output_dir>`

One-time (re-runnable) conversion: reads the master "LISTADO PROYECTOS
POR AÑOS" workbook — one sheet per year — and writes a separate,
single-sheet workbook per year (2024.xlsx / 2025.xlsx / 2026.xlsx, only
for years that actually have a sheet in the source) into <output_dir>.
This is what the Proyectos Info page actually reads from — see
project_info_dir in config/config.yaml — not the master file directly.

Re-run this any time the master workbook is updated; it always
overwrites the same 3 files, so config/config.yaml's project_info_dir
never needs to change.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.project_info.reader import split_workbook_by_year


def main():
    if len(sys.argv) != 3:
        print("Uso: python scripts/split_project_info_by_year.py <origen.xlsx> <carpeta_salida>")
        sys.exit(1)

    source_path, output_dir = sys.argv[1], sys.argv[2]
    written = split_workbook_by_year(source_path, output_dir)

    if not written:
        print("No se encontró ningún año (2024/2025/2026) en el archivo origen.")
        sys.exit(1)

    print(f"Escritos {len(written)} archivo(s) en {output_dir}:")
    for path in written:
        print(f"  {path}")


if __name__ == "__main__":
    main()