"""fill_responsiva.py
Genera una responsiva PDF rellenando texto sobre una plantilla de 3 páginas
(plantilla con encabezado/banner ya presente).

Uso:
  python fill_responsiva.py --numero 123
  python fill_responsiva.py --data-file row.json --out RESPONSIVA_123.pdf

Requisitos: reportlab, PyPDF2 (pip install reportlab PyPDF2)

Coloca la plantilla en: template_3pages.pdf
Datos: empleados.json (lista de objetos) o pasar JSON con --data-file
"""
import io
import json
import argparse
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.units import mm
import pdfrw
from pdfrw import PdfReader as PDfReader, PdfWriter as PDfWriter, PageMerge

PROJ = Path(__file__).parent
TEMPLATE = PROJ / 'templates' / 'template_3pages.pdf'
# Candidate data files (search order)
EMPLOYEES_CANDIDATES = [
    PROJ / 'empleados.json',
    PROJ / 'empleados.csv',
    PROJ / 'usuarios.json',
    PROJ / 'usuarios.csv',
    PROJ / 'PLANTILLA_PRUEBA2.xlsx',
]

# Coordenadas mapeadas (x,y) en puntos. Ajustar según tu plantilla.
# Origin (0,0) is bottom-left. These are example positions; puede que necesiten corrección.
FIELD_POSITIONS = {
    # page index 0-based
    0: {
        'nombre': (40*mm, 235*mm),
        'identidad': (40*mm, 225*mm),
        'telefono': (40*mm, 215*mm),
        'empresa': (40*mm, 205*mm),
        'descripcion': (40*mm, 190*mm),
        'placa': (40*mm, 180*mm),
        'fecha': (40*mm, 170*mm),
        'fecha_dev': (120*mm, 170*mm),
        'observaciones': (40*mm, 150*mm),
    },
    1: {
        # second page fields (if any)
        'dispositivo': (40*mm, 230*mm),
        'marca': (40*mm, 220*mm),
        'modelo': (40*mm, 210*mm),
        'serie': (40*mm, 200*mm),
    },
    2: {
        # third page (signatures, etc.)
        'firma_responsable': (40*mm, 80*mm),
        'firma_km': (120*mm, 80*mm),
    }
}

FONT_NAME = 'Helvetica'
FONT_SIZE = 10


def load_data_by_numero(numero: str):
    numero = str(numero).strip()
    # search in candidate files (JSON, CSV, XLSX)
    for candidate in EMPLOYEES_CANDIDATES:
        try:
            if not candidate.exists():
                continue
            suffix = candidate.suffix.lower()
            if suffix == '.json':
                data = json.loads(candidate.read_text(encoding='utf8'))
                if isinstance(data, dict):
                    # single object
                    data = [data]
                for r in data:
                    if str(r.get('numero','')).strip() == numero or str(r.get('id','')).strip() == numero:
                        return r
            elif suffix == '.csv':
                # prefer pandas if available
                try:
                    import pandas as pd
                    df = pd.read_csv(candidate, dtype=str)
                    for _, row in df.iterrows():
                        if str(row.get('numero','')).strip() == numero or str(row.get('id','')).strip() == numero:
                            return row.dropna().to_dict()
                except Exception:
                    import csv
                    with candidate.open('r', encoding='utf8', errors='ignore') as fh:
                        reader = csv.DictReader(fh)
                        for r in reader:
                            if str(r.get('numero','')).strip() == numero or str(r.get('id','')).strip() == numero:
                                return r
            elif suffix in ('.xls', '.xlsx'):
                try:
                    import pandas as pd
                    df = pd.read_excel(candidate, dtype=str, engine='openpyxl')
                    for _, row in df.iterrows():
                        if str(row.get('numero','')).strip() == numero or str(row.get('id','')).strip() == numero:
                            return row.dropna().to_dict()
                except Exception:
                    # cannot parse xlsx
                    continue
        except Exception:
            continue
    raise FileNotFoundError(f'No matching record for numero {numero} in candidate files')


def render_overlay(page_size, fields: dict):
    """Genera un PDF en memoria con los textos a superponer en la página"""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=page_size)
    c.setFont(FONT_NAME, FONT_SIZE)

    # determine page number from fields (fields keys like 'page') are not necessary
    for key, value in fields.items():
        pos = fields_positions_for_current_page.get(key)
        if pos and value is not None:
            x, y = pos
            c.drawString(x, y, str(value))

    c.save()
    buf.seek(0)
    return buf


def create_filled_pdf(record: dict, out_path: Path):
    # open template
    if not TEMPLATE.exists():
        raise FileNotFoundError(f'Template not found: {TEMPLATE}')

    # Use pdfrw to preserve template graphics and avoid overlay white backgrounds
    template_pdf = PDfReader(str(TEMPLATE))

    for page_index, tpl_page in enumerate(template_pdf.pages):
        mediabox = tpl_page.MediaBox
        # compute page size for overlay
        if mediabox:
            page_size = (float(mediabox[2]), float(mediabox[3]))
        else:
            from reportlab.lib.pagesizes import A4
            page_size = A4

        # prepare fields for this page
        page_fields = {}
        positions = FIELD_POSITIONS.get(page_index, {})
        for k, pos in positions.items():
            aliases = {
                'nombre': ['nombre', 'name'],
                'identidad': ['identidad', 'dpi', 'id'],
                'telefono': ['telefono', 'phone'],
                'empresa': ['empresa', 'direccion', 'company'],
                'descripcion': ['descripcion', 'descripcion_bien', 'descripcion_equipo'],
                'placa': ['placa', 'serie', 'imei'],
                'fecha': ['fecha', 'fecha_entrega'],
                'fecha_dev': ['fecha_dev', 'fecha_devolucion'],
                'observaciones': ['observaciones', 'obs', 'notas'],
                'dispositivo': ['dispositivo', 'tipo'],
                'marca': ['marca', 'brand'],
                'modelo': ['modelo', 'model'],
                'serie': ['serie', 'serial'],
            }
            value = None
            for alias in aliases.get(k, [k]):
                if alias in record and record.get(alias) not in (None, ''):
                    value = record.get(alias)
                    break
            page_fields[k] = value

        global fields_positions_for_current_page
        fields_positions_for_current_page = positions

        overlay_stream = render_overlay(page_size, page_fields)
        overlay_pdf = PDfReader(fdata=overlay_stream.getvalue())
        overlay_page = overlay_pdf.pages[0]

        PageMerge(tpl_page).add(overlay_page, viewrect=None)

    # write merged PDF
    out_path.parent.mkdir(parents=True, exist_ok=True)
    PDfWriter().write(str(out_path), template_pdf)

    print('PDF creado:', out_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--numero', help='Número o id del empleado/dispositivo a generar')
    parser.add_argument('--data-file', help='Archivo JSON con el objeto a usar (alternativa a --numero)')
    parser.add_argument('--out', help='Archivo de salida', default=None)
    args = parser.parse_args()

    record = None
    if args.data_file:
        p = Path(args.data_file)
        if not p.exists():
            raise FileNotFoundError(p)
        record = json.loads(p.read_text(encoding='utf8'))
    elif args.numero:
        record = load_data_by_numero(args.numero)
    else:
        # interactive example: try to load first record from candidate files
        record = None
        for candidate in EMPLOYEES_CANDIDATES:
            try:
                if candidate.exists():
                    # try JSON first
                    if candidate.suffix.lower() == '.json':
                        data_list = json.loads(candidate.read_text(encoding='utf8'))
                        if isinstance(data_list, list) and len(data_list) > 0:
                            record = data_list[0]
                            break
                    elif candidate.suffix.lower() == '.csv':
                        try:
                            import pandas as pd
                            df = pd.read_csv(candidate, dtype=str)
                            if not df.empty:
                                record = df.iloc[0].dropna().to_dict()
                                break
                        except Exception:
                            import csv
                            with candidate.open('r', encoding='utf8') as fh:
                                reader = csv.DictReader(fh)
                                rows = list(reader)
                                if rows:
                                    record = rows[0]
                                    break
                    elif candidate.suffix.lower() in ('.xls', '.xlsx'):
                        try:
                            import pandas as pd
                            df = pd.read_excel(candidate, dtype=str, engine='openpyxl')
                            if not df.empty:
                                record = df.iloc[0].dropna().to_dict()
                                break
                        except Exception:
                            continue
            except Exception:
                continue
        if record is None:
            raise SystemExit('Proporciona --numero o --data-file con los datos a insertar (o añade empleados.json/usuarios.json/PLANTILLA_PRUEBA2.xlsx)')

    out = Path(args.out) if args.out else PROJ / f"RESPONSIVA_{record.get('numero','output')}.pdf"
    create_filled_pdf(record, out)
