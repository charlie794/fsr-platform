# Sensor_Testor/app_io/plan_reader.py
from __future__ import annotations
import csv, gzip, io, os
from typing import List, Tuple, Optional
from xml.etree import ElementTree as ET

GZIP_MAGIC = b"\x1f\x8b"

# ----------------- basic readers -----------------

def _is_gzip(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(2)
        return head == GZIP_MAGIC
    except Exception:
        return False

def _read_text_multiencoding(path: str) -> str:
    # Try encodings in order. Many CSVs from Excel are UTF-16LE.
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    # Fallback: binary decode best-effort
    with open(path, "rb") as f:
        return f.read().decode("latin-1", errors="replace")

def _rows_from_csv_text(text: str) -> List[List[str]]:
    out: List[List[str]] = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        out.append([(str(c).strip() if c is not None else "") for c in row])
    return out

def _rows_from_gnumeric(path: str) -> List[List[str]]:
    """
    Parse a .gnumeric (gzipped XML). We map Cell[@Row, @Col] -> text and
    build a dense rectangle from (0..max_row, 0..max_col).
    """
    with gzip.open(path, "rb") as gz:
        data = gz.read()
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise RuntimeError(f"Gnumeric parse error: {e}")

    def _tag(elem, name):
        return elem.tag.endswith(name)

    cells = []
    for sheet in root.iter():
        if _tag(sheet, "Sheet"):
            for maybe_cells in sheet:
                if _tag(maybe_cells, "Cells"):
                    for cell in maybe_cells:
                        if not _tag(cell, "Cell"):
                            continue
                        r = int(cell.attrib.get("Row", "0"))
                        c = int(cell.attrib.get("Col", "0"))
                        val = ""
                        for child in cell:
                            if _tag(child, "Value") or _tag(child, "v"):
                                val = (child.text or "").strip()
                                break
                            if _tag(child, "String"):
                                val = (child.text or "").strip()
                                break
                        cells.append((r, c, val))
                    break
            break

    if not cells:
        return []

    max_r = max(r for r, _, _ in cells)
    max_c = max(c for _, c, _ in cells)
    grid = [["" for _ in range(max_c + 1)] for _ in range(max_r + 1)]
    for r, c, v in cells:
        grid[r][c] = (v or "").strip()
    return grid

def read_rows(path: str) -> List[List[str]]:
    """
    Return raw rows (list[list[str]]) for CSV or Gnumeric (.gnumeric or gzipped).
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".gnumeric" or _is_gzip(path):
        return _rows_from_gnumeric(path)
    else:
        text = _read_text_multiencoding(path)
        return _rows_from_csv_text(text)

# ----------------- table + plugin extraction -----------------

def _norm_cell(s: str) -> str:
    if s is None:
        return ""
    # strip BOMs/quotes/spaces and lowercase
    return str(s).replace("\ufeff", "").strip().strip('"').strip("'").lower()

def _looks_like_test_header(row: List[str]) -> bool:
    """True if any cell in the row equals 'test' (case-insensitive, trimmed)."""
    for c in row:
        if _norm_cell(c) == "test":
            return True
    return False

def extract_table_and_plugin(rows: List[List[str]]) -> Tuple[List[List[str]], Optional[str]]:
    """
    From the parsed plan rows (list of lists), return:
      - table_rows: header + all following rows (the grid test table)
      - plugin_name: the FIRST DATA ROW's LAST COLUMN (typically the P/F Criteria filename)

    Robust to stray whitespace/quotes/BOM; strictly ignores settings above the table.
    """
    if not rows:
        return [], None

    # 1) find header index
    header_idx = None
    for i, row in enumerate(rows):
        if row and _looks_like_test_header(row):
            header_idx = i
            break
    if header_idx is None:
        # No clear header; return everything as-is and no plugin
        return rows, None

    # 2) Build table rows from header onward, skipping leading blank data rows if any
    table_rows: List[List[str]] = []
    table_rows.append(rows[header_idx])  # header itself

    # Find first non-empty data row after header
    first_data_idx = None
    for j in range(header_idx + 1, len(rows)):
        r = rows[j]
        if r and any((c or "").strip() for c in r):
            first_data_idx = j
            break

    if first_data_idx is None:
        # No data rows; just return header
        return table_rows, None

    # Append all rows from first_data_idx onward (you can trim trailing empties if needed)
    table_rows.extend(rows[first_data_idx:])

    # 3) plugin is the *last column* of the first data row
    first_data = rows[first_data_idx]
    plugin_name = None
    if first_data:
        last_val = first_data[-1] if len(first_data) else ""
        plugin_name = (last_val or "").replace("\ufeff", "").strip().strip('"').strip("'")
        if not plugin_name:
            plugin_name = None

    return table_rows, plugin_name
