"""fill_responsiva_mupdf.py
Rellena texto sobre una plantilla PDF de 3 páginas usando PyMuPDF (fitz).
Recomendado si pdfrw/PyPDF2 están tapando el logo.

Uso:
  pip install PyMuPDF pandas openpyxl
  python fill_responsiva_mupdf.py --data-file ejemplo.json --out RESPONSIVA_mupdf_123.pdf --no-static
  python fill_responsiva_mupdf.py --numero 123 --out RESPONSIVA_mupdf_123.pdf

Colocar plantilla en templates/template_3pages.pdf
"""
import fitz
import json
import argparse
from pathlib import Path

PROJ = Path(__file__).parent
TEMPLATE = PROJ / 'templates' / 'template_3pages.pdf'
EMPLOYEES_CANDIDATES = [PROJ / 'empleados.json', PROJ / 'usuarios.json', PROJ / 'PLANTILLA_PRUEBA2.xlsx']

# mm to points
MM = 72.0 / 25.4

# Positions in mm from bottom-left of page (approx). Adjust if needed.
FIELD_POSITIONS = {
    0: {
        'nombre': (40*MM, 220*MM),
        'identidad': (40*MM, 208*MM),
        'telefono': (40*MM, 196*MM),
        'empresa': (40*MM, 184*MM),
        'descripcion': (40*MM, 160*MM),
        'placa': (40*MM, 148*MM),
        'fecha': (40*MM, 136*MM),
        'fecha_dev': (120*MM, 136*MM),
        'observaciones': (40*MM, 120*MM),
    },
    1: {
        'dispositivo': (40*MM, 230*MM),
        'marca': (40*MM, 218*MM),
        'modelo': (40*MM, 206*MM),
        'serie': (40*MM, 194*MM),
    },
    2: {
        'firma_responsable': (50*MM, 70*MM),
        'firma_km': (140*MM, 70*MM),
    }
}

FONT_NAME = 'helv'  # builtin
FONT_SIZE = 10


def load_data_by_numero(numero: str):
    numero = str(numero).strip()
    for candidate in EMPLOYEES_CANDIDATES:
        try:
            if not candidate.exists():
                continue
            if candidate.suffix.lower() == '.json':
                data = json.loads(candidate.read_text(encoding='utf8'))
                if isinstance(data, dict):
                    data = [data]
                for r in data:
                    if str(r.get('numero','')).strip() == numero or str(r.get('id','')).strip() == numero:
                        return r
            elif candidate.suffix.lower() in ('.xls', '.xlsx'):
                try:
                    import pandas as pd
                    df = pd.read_excel(candidate, dtype=str, engine='openpyxl')
                    for _, row in df.iterrows():
                        if str(row.get('numero','')).strip() == numero or str(row.get('id','')).strip() == numero:
                            return row.dropna().to_dict()
                except Exception:
                    continue
        except Exception:
            continue
    raise FileNotFoundError(f'No matching record for numero {numero}')


def load_record_from_file(path: Path):
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() == '.json':
        return json.loads(path.read_text(encoding='utf8'))
    if path.suffix.lower() in ('.xls', '.xlsx'):
        import pandas as pd
        df = pd.read_excel(path, dtype=str, engine='openpyxl')
        if df.empty:
            raise SystemExit('Excel vacío')
        return df.iloc[0].dropna().to_dict()
    raise SystemExit('Formarto no soportado para --data-file')


def draw_text_on_page(page, x_pt, y_pt, text, fontsize=FONT_SIZE):
    # fitz origin is top-left. Convert y from bottom-left to top-left
    page_height = page.rect.height
    y_top = page_height - y_pt
    # small rectangle to draw into
    rect = fitz.Rect(x_pt, y_top - fontsize*1.2, x_pt + 400, y_top)
    page.insert_textbox(rect, str(text), fontsize=fontsize, fontname=FONT_NAME, fill=(0,0,0))


def create_filled_pdf(record: dict, out_path: Path, skip_static=False):
    if not TEMPLATE.exists():
        raise FileNotFoundError(f'Template not found: {TEMPLATE}')

    doc = fitz.open(str(TEMPLATE))

    # For each page, write dynamic fields
    for page_index in range(len(doc)):
        page = doc[page_index]
        positions = FIELD_POSITIONS.get(page_index, {})
        for key, pos in positions.items():
            value = None
            # aliases
            aliases = {
                'nombre': ['nombre','name'],
                'identidad': ['identidad','dpi','id'],
                'telefono': ['telefono','phone'],
                'empresa': ['empresa','direccion','company'],
                'descripcion': ['descripcion','descripcion_bien','descripcion_equipo'],
                'placa': ['placa','serie','imei'],
                'fecha': ['fecha','fecha_entrega'],
                'fecha_dev': ['fecha_dev','fecha_devolucion'],
                'observaciones': ['observaciones','obs','notas'],
                'dispositivo': ['dispositivo','tipo'],
                'marca': ['marca','brand'],
                'modelo': ['modelo','model'],
                'serie': ['serie','serial'],
            }
            for alias in aliases.get(key, [key]):
                if alias in record and record.get(alias) not in (None, ''):
                    value = record.get(alias)
                    break
            if value is not None:
                x_pt, y_pt = pos
                draw_text_on_page(page, x_pt, y_pt, value)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    doc.close()
    print('PDF creado:', out_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--numero', help='Número o id del empleado/dispositivo a generar')
    parser.add_argument('--data-file', help='Archivo JSON/XLSX con el objeto a usar (alternativa a --numero)')
    parser.add_argument('--out', help='Archivo de salida', default=None)
    parser.add_argument('--no-static', action='store_true', help='No añadir textos estáticos en caso de plantilla')
    args = parser.parse_args()

    record = None
    if args.data_file:
        record = load_record_from_file(Path(args.data_file))
    elif args.numero:
        record = load_data_by_numero(args.numero)
    else:
        # try first candidate JSON
        for cand in EMPLOYEES_CANDIDATES:
            if cand.exists() and cand.suffix.lower() == '.json':
                data = json.loads(cand.read_text(encoding='utf8'))
                record = data[0] if isinstance(data, list) else data
                break
        if record is None:
            raise SystemExit('Proporciona --numero o --data-file con los datos a usar')

    out = Path(args.out) if args.out else PROJ / f"RESPONSIVA_mupdf_{record.get('numero','output')}.pdf"
    create_filled_pdf(record, out, skip_static=args.no_static)
