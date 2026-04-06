import streamlit as st
import pandas as pd
import warnings

warnings.filterwarnings("ignore", category=FutureWarning)

import google.generativeai as genai
import json
import pickle
from datetime import datetime, timedelta
import os
import requests
import re
import time
import unicodedata

try:
    from fpdf import FPDF
except ImportError:
    FPDF = None


ARCHIVO_TASA_CACHE = "tasa_usd_hnl_cache.json"
ARCHIVO_TASA_HISTORIAL = "tasa_usd_hnl_historial.json"
PORCENTAJE_ALERTA_VARIACION = 0.5  # Alerta si cambia >= 0.5% respecto al ultimo registro del dia


def _texto_seguro_pdf(valor, max_fragmento=30):
    """Normaliza texto para FPDF evitando saltos/problematicos y palabras demasiado largas."""
    texto = str(valor or "").replace("\r", " ").replace("\n", " ").strip()
    texto = re.sub(r"\s+", " ", texto)
    if not texto:
        return ""

    fragmentos = []
    for token in texto.split(" "):
        if len(token) <= max_fragmento:
            fragmentos.append(token)
        else:
            fragmentos.extend(token[i:i + max_fragmento] for i in range(0, len(token), max_fragmento))

    return " ".join(fragmentos).encode("latin-1", "replace").decode("latin-1")


def _registrar_tasa_historial(tasa, fuente):
    """Agrega una entrada al historial diario de la tasa USD->HNL."""
    historial = []
    if os.path.exists(ARCHIVO_TASA_HISTORIAL):
        try:
            with open(ARCHIVO_TASA_HISTORIAL, "r", encoding="utf-8") as f:
                historial = json.load(f)
            if not isinstance(historial, list):
                historial = []
        except Exception:
            historial = []

    entrada = {
        "tasa": round(float(tasa), 6),
        "fuente": str(fuente).strip() or "desconocida",
        "fecha": datetime.now().strftime("%Y-%m-%d"),
        "hora": datetime.now().strftime("%H:%M:%S"),
    }
    # Evitar duplicado exacto del mismo valor el mismo dia
    if historial:
        ultimo = historial[-1]
        if ultimo.get("fecha") == entrada["fecha"] and abs(ultimo.get("tasa", 0) - entrada["tasa"]) < 1e-9:
            return
    historial.append(entrada)
    # Conservar solo los ultimos 365 registros para no crecer indefinidamente
    historial = historial[-365:]
    try:
        with open(ARCHIVO_TASA_HISTORIAL, "w", encoding="utf-8") as f:
            json.dump(historial, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _verificar_alerta_variacion(tasa_nueva):
    """Devuelve un mensaje de alerta si la tasa cambio significativamente hoy."""
    if not os.path.exists(ARCHIVO_TASA_HISTORIAL):
        return None
    try:
        with open(ARCHIVO_TASA_HISTORIAL, "r", encoding="utf-8") as f:
            historial = json.load(f)
        if not historial:
            return None
        hoy = datetime.now().strftime("%Y-%m-%d")
        # Primera lectura del dia
        primeras_hoy = [e for e in historial if e.get("fecha") == hoy]
        if len(primeras_hoy) < 1:
            return None
        tasa_base = primeras_hoy[0]["tasa"]
        if tasa_base == 0:
            return None
        variacion_pct = abs(tasa_nueva - tasa_base) / tasa_base * 100
        if variacion_pct >= PORCENTAJE_ALERTA_VARIACION:
            signo = "â–²" if tasa_nueva > tasa_base else "â–¼"
            return (
                f"{signo} Variacion de {variacion_pct:.4f}% respecto a la tasa inicial del dia "
                f"({tasa_base:.4f} -> {tasa_nueva:.4f} HNL/USD)"
            )
    except Exception:
        pass
    return None


def _normalizar_tasa(valor):
    """Valida que la tasa sea numerica y razonable para USD->HNL."""
    try:
        tasa = float(valor)
    except (TypeError, ValueError):
        return None

    if 15.0 <= tasa <= 50.0:
        return round(tasa, 6)
    return None


def _leer_tasa_cache():
    """Lee cache local de tasa si existe."""
    if not os.path.exists(ARCHIVO_TASA_CACHE):
        return None

    try:
        with open(ARCHIVO_TASA_CACHE, "r", encoding="utf-8") as f:
            data = json.load(f)
        tasa = _normalizar_tasa(data.get("tasa"))
        actualizado_en = data.get("actualizado_en")
        fuente = str(data.get("fuente", "cache local")).strip() or "cache local"
        if tasa and actualizado_en:
            return {
                "tasa": tasa,
                "fuente": fuente,
                "actualizado_en": actualizado_en,
            }
    except Exception:
        return None

    return None


def _guardar_tasa_cache(tasa, fuente):
    """Guarda tasa en disco para persistencia entre reinicios."""
    payload = {
        "tasa": float(tasa),
        "fuente": str(fuente).strip() or "desconocida",
        "actualizado_en": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(ARCHIVO_TASA_CACHE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _obtener_tasa_desde_fuentes():
    """Consulta varias fuentes de tasa USD->HNL y devuelve la primera valida."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    fuentes = [
        ("open.er-api.com", "https://open.er-api.com/v6/latest/USD", "json_rates_hnl"),
        ("frankfurter.app", "https://api.frankfurter.app/latest?from=USD&to=HNL", "json_rates_hnl"),
        ("ExchangeRate-API", "https://api.exchangerate-api.com/v4/latest/USD", "json_rates_hnl"),
        ("Google (fallback)", "https://www.google.com/search?q=usd+to+hnl", "google_regex"),
    ]

    for fuente, url, tipo in fuentes:
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if not r.ok:
                continue

            tasa = None
            if tipo == "json_rates_hnl":
                data = r.json()
                tasa = _normalizar_tasa((data.get("rates") or {}).get("HNL"))
            elif tipo == "google_regex":
                m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*HNL", r.text)
                if m:
                    tasa = _normalizar_tasa(m.group(1))

            if tasa is not None:
                return tasa, fuente
        except Exception:
            continue

    return None, None


def obtener_tasa_usd_hnl(force_refresh=False, max_horas_cache=6):
    """Obtiene tasa USD->HNL con cache local y multiples fuentes online."""
    cache = _leer_tasa_cache()

    if cache and not force_refresh:
        try:
            actualizado = datetime.strptime(cache["actualizado_en"], "%Y-%m-%d %H:%M:%S")
            if datetime.now() - actualizado <= timedelta(hours=max_horas_cache):
                return cache["tasa"], cache["fuente"], cache["actualizado_en"]
        except Exception:
            pass

    tasa, fuente = _obtener_tasa_desde_fuentes()
    if tasa is not None:
        _guardar_tasa_cache(tasa, fuente)
        _registrar_tasa_historial(tasa, fuente)
        actualizado_en = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return tasa, fuente, actualizado_en

    # Si no hay internet/fuente, se usa el ultimo valor local guardado.
    if cache:
        return cache["tasa"], f"{cache['fuente']} (cache local)", cache["actualizado_en"]

    return None, None, None

# ============ CONFIGURACIÃ“N DE TEMA ============
def apply_theme(tema):
    """Aplica el tema seleccionado"""
    if tema == "Oscuro":
        st.markdown("""
        <style>
            .stApp { background-color: #0e1117; color: #c9d1d9; }
            .stSidebar { background-color: #161b22; }
        </style>
        """, unsafe_allow_html=True)
    elif tema == "Azul":
        st.markdown("""
        <style>
            .stApp { background-color: #001f3f; color: #e0f0ff; }
            .stSidebar { background-color: #001a33; }
            .stButton, .stDownloadButton { background-color: #0056b3; color: #ffffff; }
        </style>
        """, unsafe_allow_html=True)
    elif tema == "Verde":
        st.markdown("""
        <style>
            .stApp { background-color: #003300; color: #e6ffe6; }
            .stSidebar { background-color: #002a00; }
            .stButton, .stDownloadButton { background-color: #007f0e; color: #ffffff; }
        </style>
        """, unsafe_allow_html=True)
    elif tema == "Rojo":
        st.markdown("""
        <style>
            .stApp { background-color: #330000; color: #ffe6e6; }
            .stSidebar { background-color: #2a0000; }
            .stButton, .stDownloadButton { background-color: #b30000; color: #ffffff; }
        </style>
        """, unsafe_allow_html=True)
    elif tema == "Claro":
        st.markdown("""
        <style>
            .stApp { background-color: #ffffff; color: #24292e; }
            .stSidebar { background-color: #f6f8fa; }
        </style>
        """, unsafe_allow_html=True)


def apply_main_background(mode="Impacto"):
    """Aplica un fondo visual tipo cielo tecnologico con iconos decorativos."""
    is_sutil = str(mode).strip().lower() == "sutil"
    is_corporativo = str(mode).strip().lower() == "corporativo"

    if is_corporativo:
        grad_1 = "radial-gradient(circle at 16% 14%, rgba(158, 203, 230, 0.10) 0, rgba(158, 203, 230, 0.0) 35%)"
        grad_2 = "radial-gradient(circle at 84% 10%, rgba(202, 224, 241, 0.08) 0, rgba(202, 224, 241, 0.0) 32%)"
        grad_3 = "linear-gradient(180deg, #0d2236 0%, #163951 42%, #1f4f70 100%)"
        icon_opacity = "0.07"
        icon_shadow = "0 3px 12px rgba(0, 0, 0, 0.18)"
        sat_size = "52px"
        wifi_size = "62px"
        phone_size = "70px"
    elif is_sutil:
        grad_1 = "radial-gradient(circle at 18% 16%, rgba(157, 211, 255, 0.12) 0, rgba(157, 211, 255, 0.0) 36%)"
        grad_2 = "radial-gradient(circle at 82% 12%, rgba(181, 229, 255, 0.10) 0, rgba(181, 229, 255, 0.0) 34%)"
        grad_3 = "linear-gradient(180deg, #082b48 0%, #0b4476 38%, #1f6ea4 100%)"
        icon_opacity = "0.10"
        icon_shadow = "0 4px 18px rgba(0, 0, 0, 0.20)"
        sat_size = "58px"
        wifi_size = "70px"
        phone_size = "78px"
    else:
        grad_1 = "radial-gradient(circle at 18% 16%, rgba(157, 211, 255, 0.22) 0, rgba(157, 211, 255, 0.0) 36%)"
        grad_2 = "radial-gradient(circle at 82% 12%, rgba(181, 229, 255, 0.20) 0, rgba(181, 229, 255, 0.0) 34%)"
        grad_3 = "linear-gradient(180deg, #083b67 0%, #0a4f8c 30%, #0e6db8 62%, #2c8fd0 100%)"
        icon_opacity = "0.16"
        icon_shadow = "0 6px 24px rgba(0, 0, 0, 0.28)"
        sat_size = "72px"
        wifi_size = "86px"
        phone_size = "94px"

    st.markdown(
        f"""
        <style>
            .stApp {{
                background: transparent !important;
            }}

            [data-testid="stAppViewContainer"] {{
                background:
                    {grad_1},
                    {grad_2},
                    {grad_3};
                background-attachment: fixed;
            }}

            .sky-tech-overlay {{
                position: fixed;
                inset: 0;
                z-index: -1;
                pointer-events: none;
                overflow: hidden;
            }}

            .sky-tech-overlay span {{
                position: absolute;
                color: rgba(235, 248, 255, {icon_opacity});
                text-shadow: {icon_shadow};
            }}

            .sky-tech-sat {{ top: 12%; left: 8%; font-size: {sat_size}; transform: rotate(-12deg); }}
            .sky-tech-wifi {{ top: 34%; right: 10%; font-size: {wifi_size}; transform: rotate(8deg); }}
            .sky-tech-phone {{ bottom: 10%; left: 44%; font-size: {phone_size}; transform: rotate(-6deg); }}
        </style>
        <div class="sky-tech-overlay">
            <span class="sky-tech-sat">ðŸ›°ï¸</span>
            <span class="sky-tech-wifi">ðŸ“¶</span>
            <span class="sky-tech-phone">ðŸ“±</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ============ GESTIÃ“N DE USUARIOS ============
class GestorUsuarios:
    def __init__(self, archivo="usuarios.json"):
        self.archivo = archivo
        self.usuarios = self.cargar_usuarios()
    
    def cargar_usuarios(self):
        if os.path.exists(self.archivo):
            with open(self.archivo, 'r', encoding='utf-8') as f:
                usuarios = json.load(f)

            # Normaliza usuarios antiguos que no tienen el permiso explÃ­cito.
            for _, data in usuarios.items():
                if "puede_editar" not in data:
                    data["puede_editar"] = data.get("rol", "usuario") in ["administrador", "superadministrador", "usuario"]
                if "permisos" not in data:
                    puede_total = bool(data.get("puede_editar", False) or data.get("rol") in ["administrador", "superadministrador"])
                    data["permisos"] = {
                        "crear": puede_total,
                        "editar": puede_total,
                        "eliminar": puede_total,
                        "importar": puede_total,
                        "exportar": True,
                    }

            # Garantiza cuentas administrativas mÃ­nimas si el JSON solo trae usuarios de prueba.
            existe_superadmin = any(u.get("rol") == "superadministrador" for u in usuarios.values())
            existe_admin = any(u.get("rol") == "administrador" for u in usuarios.values())

            if not existe_superadmin:
                usuarios["superadmin"] = {
                    "contraseÃ±a": "super123",
                    "rol": "superadministrador",
                    "email": "super@empresa.com",
                    "puede_editar": True,
                    "permisos": {
                        "crear": True,
                        "editar": True,
                        "eliminar": True,
                        "importar": True,
                        "exportar": True,
                    }
                }

            if not existe_admin:
                usuarios["admin"] = {
                    "contraseÃ±a": "admin123",
                    "rol": "administrador",
                    "email": "admin@empresa.com",
                    "puede_editar": True,
                    "permisos": {
                        "crear": True,
                        "editar": True,
                        "eliminar": True,
                        "importar": True,
                        "exportar": True,
                    }
                }

            if not existe_superadmin or not existe_admin:
                with open(self.archivo, 'w', encoding='utf-8') as f:
                    json.dump(usuarios, f, ensure_ascii=False, indent=2)
            return usuarios

        return {
            "superadmin": {
                "contraseÃ±a": "super123",
                "rol": "superadministrador",
                "email": "super@empresa.com",
                "puede_editar": True,
                "permisos": {
                    "crear": True,
                    "editar": True,
                    "eliminar": True,
                    "importar": True,
                    "exportar": True,
                }
            },
            "admin": {
                "contraseÃ±a": "admin123",
                "rol": "administrador",
                "email": "admin@empresa.com",
                "puede_editar": True,
                "permisos": {
                    "crear": True,
                    "editar": True,
                    "eliminar": True,
                    "importar": True,
                    "exportar": True,
                }
            }
        }
    
    def guardar_usuarios(self):
        with open(self.archivo, 'w', encoding='utf-8') as f:
            json.dump(self.usuarios, f, ensure_ascii=False, indent=2)
    
    def crear_usuario(self, usuario, contraseÃ±a, rol="usuario", email="", puede_editar=True):
        if usuario in self.usuarios:
            return False, "El usuario ya existe"

        if rol not in ["usuario", "administrador", "superadministrador"]:
            return False, "Rol no vÃ¡lido"
        
        self.usuarios[usuario] = {
            "contraseÃ±a": contraseÃ±a,
            "rol": rol,
            "email": email,
            "puede_editar": bool(puede_editar),
            "permisos": {
                "crear": bool(puede_editar),
                "editar": bool(puede_editar),
                "eliminar": bool(puede_editar),
                "importar": bool(puede_editar),
                "exportar": True,
            },
            "fecha_creacion": datetime.now().isoformat()
        }
        self.guardar_usuarios()
        return True, "Usuario creado exitosamente"

    def actualizar_usuario(self, usuario, contraseÃ±a=None, rol=None, email=None, puede_editar=None, permisos=None):
        if usuario not in self.usuarios:
            return False, "Usuario no encontrado"

        if rol and rol not in ["usuario", "administrador", "superadministrador"]:
            return False, "Rol no vÃ¡lido"

        if contraseÃ±a:
            self.usuarios[usuario]["contraseÃ±a"] = contraseÃ±a
        if rol:
            self.usuarios[usuario]["rol"] = rol
        if email is not None:
            self.usuarios[usuario]["email"] = email
        if puede_editar is not None:
            self.usuarios[usuario]["puede_editar"] = bool(puede_editar)
        if permisos is not None and isinstance(permisos, dict):
            self.usuarios[usuario]["permisos"] = permisos

        self.guardar_usuarios()
        return True, "Usuario actualizado exitosamente"
    
    def validar_usuario(self, usuario, contraseÃ±a):
        if usuario not in self.usuarios:
            return False, "Usuario no encontrado"
        
        if self.usuarios[usuario]["contraseÃ±a"] != contraseÃ±a:
            return False, "ContraseÃ±a incorrecta"
        
        return True, self.usuarios[usuario]
    
    def obtener_usuarios(self):
        return list(self.usuarios.keys())
    
    def eliminar_usuario(self, usuario):
        if usuario in ["admin", "superadmin"]:
            return False, "No puedes eliminar cuentas administrativas protegidas"
        
        if usuario in self.usuarios:
            del self.usuarios[usuario]
            self.guardar_usuarios()
            return True, "Usuario eliminado"
        
        return False, "Usuario no encontrado"

# Configurar pÃ¡gina
st.set_page_config(
    page_title="Dashboard - GestiÃ³n de Planes Corporativos",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Inicializar gestor de usuarios
gestor_usuarios = GestorUsuarios()

# Funciones de persistencia para planes
PLANES_FILE = "planes.json"

def cargar_planes():
    if os.path.exists(PLANES_FILE):
        try:
            with open(PLANES_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    return []


MOVIMIENTOS_FILE = "movimientos.json"
DEBUG_IMPORT_FILE = "debug_importacion.log"

def cargar_movimientos():
    if os.path.exists(MOVIMIENTOS_FILE):
        try:
            with open(MOVIMIENTOS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    return []


def guardar_planes():
    try:
        with open(PLANES_FILE, 'w', encoding='utf-8') as f:
            json.dump(st.session_state.planes, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        st.error(f"âŒ No se pudo guardar planes: {e}")


def registrar_debug_importacion(evento, detalle):
    try:
        with open(DEBUG_IMPORT_FILE, 'a', encoding='utf-8') as f:
            marca = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{marca}] {evento}: {detalle}\n")
    except Exception:
        pass


# Inicializar sesiÃ³n
if 'planes' not in st.session_state:
    st.session_state.planes = cargar_planes()

if 'usuario_actual' not in st.session_state:
    st.session_state.usuario_actual = None

if 'puede_editar' not in st.session_state:
    st.session_state.puede_editar = False

if 'archivo_cargado' not in st.session_state:
    st.session_state.archivo_cargado = False

if 'tema' not in st.session_state:
    st.session_state.tema = "Oscuro"

if 'movimientos' not in st.session_state:
    st.session_state.movimientos = cargar_movimientos()

if 'mostrar_numeros_corporativos' not in st.session_state:
    st.session_state.mostrar_numeros_corporativos = False


def registrar_movimiento(tipo, detalle):
    st.session_state.movimientos.append({
        "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "usuario": st.session_state.usuario_actual or "Anonimo",
        "tipo": tipo,
        "detalle": detalle
    })

    # tambiÃ©n guardar en archivo para persistencia si quieres
    try:
        with open(MOVIMIENTOS_FILE, 'w', encoding='utf-8') as f:
            json.dump(st.session_state.movimientos, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def tiene_permiso(accion):
    """Valida permisos finos por accion para el usuario en sesion."""
    if st.session_state.get("rol") in ["administrador", "superadministrador"]:
        return True

    usuario = st.session_state.get("usuario_actual")
    data = gestor_usuarios.usuarios.get(usuario, {})
    permisos = data.get("permisos", {})
    return bool(permisos.get(accion, False))


def resumen_numeros_corporativos(planes):
    """Calcula totales de lineas corporativas (totales, asignadas y disponibles)."""
    total_lineas = len(planes)
    lineas_asignadas = 0

    for plan in planes:
        nombre = str(plan.get("nombre_personal", "")).strip()
        if nombre and nombre.lower() not in ["none", "nan"]:
            lineas_asignadas += 1

    lineas_disponibles = max(total_lineas - lineas_asignadas, 0)
    return total_lineas, lineas_asignadas, lineas_disponibles


def _normalizar_texto_columna(valor):
    texto = str(valor).strip().lower()
    if not texto or texto in ["nan", "none"]:
        return ""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    texto = texto.replace(" ", "_")
    texto = re.sub(r"[^a-z0-9_]+", "", texto)
    return texto


def normalizar_numero_telefonico(numero):
    """Normaliza numeros corporativos a formato +504XXXXXXXX cuando aplica."""
    texto = str(numero).strip()
    if not texto or texto.lower() in ["nan", "none"]:
        return ""

    digitos = "".join(ch for ch in texto if ch.isdigit())
    if digitos.startswith("504") and len(digitos) == 11:
        return f"+{digitos}"
    if len(digitos) == 8:
        return f"+504{digitos}"
    if texto.startswith("+") and digitos:
        return f"+{digitos}"
    return texto


def preparar_dataframe_importacion(df):
    """Normaliza encabezados para soportar plantillas CSV/Excel/Google Sheets."""
    if df is None or df.empty:
        return df

    def _normalizar_columnas(frame):
        aliases = {
            "numero": "numero",
            "numero_de_linea": "numero",
            "numero_linea": "numero",
            "telefono": "numero",
            "linea": "numero",
            "operador": "operador",
            "empresa": "operador",
            "proveedor": "operador",
            "compania": "operador",
            "carrier": "operador",
            "nombre": "nombre_personal",
            "asignado_a": "nombre_personal",
            "colaborador": "nombre_personal",
            "nombre_personal": "nombre_personal",
            "area": "area",
            "departamento": "departamento",
            "perfil": "perfil_profesional",
            "perfil_profesional": "perfil_profesional",
            "cargo": "perfil_profesional",
            "valor_usd": "valor_usd",
            "valor": "valor_usd",
            "valor_": "valor_usd",
            "valor__": "valor_usd",
            "valorusd": "valor_usd",
            "valor_en_usd": "valor_usd",
            "costo_usd": "valor_usd",
            "costo": "valor_usd",
            "usd": "valor_usd",
            "dolares": "valor_usd",
            "plan_dolares": "valor_usd",
            "tasa_usd_hnl": "tasa_usd_hnl",
            "tasa": "tasa_usd_hnl",
            "valor_hnl": "valor_hnl",
            "lempiras": "valor_hnl",
            "observaciones": "observaciones",
            "dispositivo_asignado": "dispositivo_asignado",
            "marca": "marca",
            "modelo": "modelo",
            "serie_dispositivo": "serie_dispositivo",
            "imei1": "imei1",
            "imei2": "imei2",
        }

        renames = {}
        for col in frame.columns:
            normalizada = _normalizar_texto_columna(col)
            if not normalizada:
                continue
            renames[col] = aliases.get(normalizada, normalizada)
        frame = frame.rename(columns=renames)
        frame.columns = [str(c).strip().lower() for c in frame.columns]
        return frame

    df = _normalizar_columnas(df)

    if "numero" not in df.columns:
        max_filas_header = min(12, len(df))
        for i in range(max_filas_header):
            candidatos = [_normalizar_texto_columna(v) for v in df.iloc[i].tolist()]
            if "numero" in candidatos and (
                "area" in candidatos or "departamento" in candidatos or "valor_usd" in candidatos
            ):
                nuevos_headers = [str(v).strip() for v in df.iloc[i].tolist()]
                df = df.iloc[i + 1 :].copy()
                df.columns = nuevos_headers
                df = _normalizar_columnas(df)
                break

    if not df.empty:
        primera_col = df.columns[0]
        primer_valor = df[primera_col].astype(str).str.strip()
        mask_meta = primer_valor.str.startswith("#") | (primer_valor == "---")
        df = df[~mask_meta].reset_index(drop=True)

    return df


def analizar_importacion_lineas(df_nuevas, planes_actuales, tasa_default):
    """Calcula resumen de importaciÃ³n sin persistir cambios."""
    resumen = {
        "filas": 0,
        "nuevas": 0,
        "duplicadas": 0,
        "invalidas": 0,
    }

    if df_nuevas is None or df_nuevas.empty:
        return resumen

    resumen["filas"] = len(df_nuevas.index)
    existentes = {
        normalizar_numero_telefonico(p.get("numero", ""))
        for p in planes_actuales
    }

    for _, row in df_nuevas.iterrows():
        plan = construir_plan_desde_fila(row.to_dict(), tasa_default)
        if not plan:
            resumen["invalidas"] += 1
            continue

        numero_norm = normalizar_numero_telefonico(plan.get("numero", ""))
        if not numero_norm or numero_norm in existentes:
            resumen["duplicadas"] += 1
            continue

        resumen["nuevas"] += 1
        existentes.add(numero_norm)

    return resumen


def normalizar_y_deduplicar_planes(planes):
    """Normaliza nÃºmeros y elimina duplicados exactos por nÃºmero normalizado."""
    vistos = set()
    planes_limpios = []
    cambios_numero = 0
    duplicados_eliminados = 0

    for plan in planes:
        plan_nuevo = dict(plan)
        original = str(plan_nuevo.get("numero", "")).strip()
        normalizado = normalizar_numero_telefonico(original)
        if original != normalizado:
            cambios_numero += 1
        plan_nuevo["numero"] = normalizado

        if not normalizado:
            duplicados_eliminados += 1
            continue

        if normalizado in vistos:
            duplicados_eliminados += 1
            continue

        vistos.add(normalizado)
        planes_limpios.append(plan_nuevo)

    return planes_limpios, cambios_numero, duplicados_eliminados


def procesar_importacion_lineas_nuevas(archivo_nuevas_lineas, hoja_nuevas_lineas):
    if archivo_nuevas_lineas is None:
        st.warning("Selecciona un archivo para importar.")
        return

    try:
        nombre_archivo = archivo_nuevas_lineas.name.lower()
        registrar_debug_importacion(
            "inicio",
            f"archivo={archivo_nuevas_lineas.name}; hoja={hoja_nuevas_lineas}; puede_editar={st.session_state.puede_editar}; planes_actuales={len(st.session_state.planes)}",
        )
        archivo_nuevas_lineas.seek(0)
        if nombre_archivo.endswith(".csv"):
            df_nuevas = pd.read_csv(archivo_nuevas_lineas)
        elif nombre_archivo.endswith((".xlsx", ".xls")):
            if not hoja_nuevas_lineas:
                st.warning("Selecciona la hoja de Excel.")
                return
            archivo_nuevas_lineas.seek(0)
            df_nuevas = pd.read_excel(archivo_nuevas_lineas, sheet_name=hoja_nuevas_lineas)
        else:
            st.error("Formato no soportado.")
            return

        df_nuevas = preparar_dataframe_importacion(df_nuevas)
        registrar_debug_importacion("columnas_detectadas", f"columnas={list(df_nuevas.columns)}; filas={len(df_nuevas.index)}")

        if "numero" not in df_nuevas.columns:
            registrar_debug_importacion("error_columnas", "No se detecto columna numero")
            st.error("No se detecto la columna 'numero'. Verifica encabezados del archivo.")
            return

        tasa_default = float(st.session_state.get("tasa_usd_hnl", 24.0))
        existentes = {
            normalizar_numero_telefonico(p.get("numero", ""))
            for p in st.session_state.planes
        }
        nuevos_planes = []
        duplicados = 0
        invalidas = 0

        for _, row in df_nuevas.iterrows():
            plan = construir_plan_desde_fila(row.to_dict(), tasa_default)
            if not plan:
                invalidas += 1
                continue
            numero_norm = normalizar_numero_telefonico(plan.get("numero", ""))
            if not numero_norm or numero_norm in existentes:
                duplicados += 1
                continue
            plan["numero"] = numero_norm
            nuevos_planes.append(plan)
            existentes.add(numero_norm)

        if nuevos_planes:
            st.session_state.planes.extend(nuevos_planes)
            guardar_planes()
            registrar_debug_importacion(
                "guardado_ok",
                f"agregadas={len(nuevos_planes)}; duplicadas={duplicados}; invalidas={invalidas}; total_final={len(st.session_state.planes)}",
            )
            registrar_movimiento("Importar lineas nuevas", f"{len(nuevos_planes)} lineas agregadas; {duplicados} duplicadas; {invalidas} invalidas")
            st.success(f"Se agregaron {len(nuevos_planes)} lineas nuevas. Duplicadas: {duplicados}. Invalidas: {invalidas}.")
            st.rerun()
        else:
            registrar_debug_importacion("sin_agregados", f"agregadas=0; duplicadas={duplicados}; invalidas={invalidas}")
            registrar_movimiento("Importar lineas nuevas", f"0 lineas agregadas; {duplicados} duplicadas; {invalidas} invalidas")
            st.info(f"No se agregaron lineas nuevas. Duplicadas: {duplicados}. Invalidas: {invalidas}.")
    except Exception as e:
        registrar_debug_importacion("exception", str(e))
        st.error(f"Error en importacion de lineas nuevas: {e}")


def construir_plan_desde_fila(row, tasa_default):
    """Construye un plan minimo a partir de una fila importada."""
    numero = normalizar_numero_telefonico(row.get("numero", ""))
    if not numero:
        return None

    nombre = str(row.get("nombre_personal", "")).strip()
    if nombre.lower() in ["nan", "none"]:
        nombre = ""

    valor_usd = pd.to_numeric(row.get("valor_usd", 0.0), errors="coerce")
    if pd.isna(valor_usd):
        valor_usd = 0.0

    tasa = pd.to_numeric(row.get("tasa_usd_hnl", tasa_default), errors="coerce")
    if pd.isna(tasa) or tasa <= 0:
        tasa = tasa_default

    valor_hnl = pd.to_numeric(row.get("valor_hnl", valor_usd * tasa), errors="coerce")
    if pd.isna(valor_hnl):
        valor_hnl = valor_usd * tasa

    return {
        "numero": numero,
        "operador": str(row.get("operador", "TIGO")).strip() or "TIGO",
        "nombre_personal": nombre,
        "area": str(row.get("area", "Sin Ãrea")).strip() or "Sin Ãrea",
        "departamento": str(row.get("departamento", "Sin Departamento")).strip() or "Sin Departamento",
        "perfil_profesional": str(row.get("perfil_profesional", "")).strip(),
        "valor_usd": float(valor_usd),
        "tasa_usd_hnl": float(tasa),
        "valor_hnl": float(valor_hnl),
        "observaciones": str(row.get("observaciones", "")).strip(),
        "dispositivo_asignado": str(row.get("dispositivo_asignado", "")).strip(),
        "marca": str(row.get("marca", "")).strip(),
        "modelo": str(row.get("modelo", "")).strip(),
        "serie_dispositivo": str(row.get("serie_dispositivo", "")).strip(),
        "imei1": str(row.get("imei1", "")).strip(),
        "imei2": str(row.get("imei2", "")).strip(),
        "dispositivo_historial": row.get("dispositivo_historial", []) if isinstance(row.get("dispositivo_historial", []), list) else [],
        "fecha_creacion": str(row.get("fecha_creacion", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))),
    }


def render_hero_principal(usuario, rol):
    """Renderiza cabecera principal tipo hero con animacion suave."""
    st.markdown(
        f"""
        <style>
            .hero-wrap {{
                max-width: 420px;
                margin: 6px auto 14px auto;
                border-radius: 34px;
                padding: 34px 20px 18px;
                position: relative;
                background:
                    radial-gradient(circle at 50% 2%, rgba(165, 220, 255, 0.24), transparent 18%),
                    linear-gradient(160deg, rgba(8, 21, 36, 0.92), rgba(13, 34, 57, 0.95));
                border: 2px solid rgba(178, 225, 255, 0.34);
                box-shadow:
                    0 20px 36px rgba(0, 0, 0, 0.34),
                    inset 0 0 0 1px rgba(255, 255, 255, 0.08);
                animation: heroFadeIn 700ms ease-out;
                overflow: hidden;
            }}

            .hero-wrap::before {{
                content: "";
                position: absolute;
                top: 10px;
                left: 50%;
                transform: translateX(-50%);
                width: 12px;
                height: 12px;
                border-radius: 50%;
                background: radial-gradient(circle at 35% 30%, #9ad9ff, #0f1f35 65%);
                box-shadow: 0 0 0 3px rgba(6, 14, 24, 0.9);
            }}

            .hero-wrap::after {{
                content: "";
                position: absolute;
                bottom: 7px;
                left: 50%;
                transform: translateX(-50%);
                width: 95px;
                height: 5px;
                border-radius: 20px;
                background: rgba(188, 228, 255, 0.82);
                box-shadow: 0 0 10px rgba(149, 215, 255, 0.35);
            }}

            .hero-title {{
                margin: 0;
                font-size: clamp(24px, 3.1vw, 34px);
                line-height: 1.04;
                color: #f5fbff;
                letter-spacing: 0.7px;
                text-transform: uppercase;
                text-align: center;
            }}

            .hero-subtitle {{
                margin: 10px 0 0 0;
                color: rgba(219, 239, 255, 0.9);
                font-size: clamp(13px, 1.1vw, 15px);
                text-align: center;
            }}

            .hero-meta {{
                margin-top: 10px;
                color: rgba(216, 236, 252, 0.84);
                font-size: 12px;
                text-align: center;
                border-top: 1px solid rgba(177, 222, 255, 0.2);
                padding-top: 9px;
            }}

            @media (max-width: 640px) {{
                .hero-wrap {{
                    max-width: 100%;
                    border-radius: 30px;
                    padding: 30px 16px 16px;
                }}
            }}

            @keyframes heroFadeIn {{
                from {{ opacity: 0; transform: translateY(8px); }}
                to {{ opacity: 1; transform: translateY(0); }}
            }}
        </style>
        <section class="hero-wrap">
            <h1 class="hero-title">GESTION PLANES CORPORATIVOS</h1>
            <p class="hero-subtitle">Control centralizado de lineas y asignaciones corporativas.</p>
            <div class="hero-meta">Usuario: {usuario} | Rol: {rol}</div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def render_kpi_cards(total_lineas, gasto_total, areas_diff, deptos_diff):
    """Renderiza KPIs en tarjetas premium estilo glass."""
    st.markdown(
        f"""
        <style>
            .kpi-grid {{
                display: grid;
                grid-template-columns: repeat(4, minmax(0, 1fr));
                gap: 12px;
                margin-bottom: 10px;
            }}

            .kpi-card {{
                border-radius: 14px;
                padding: 14px 14px 12px;
                background: linear-gradient(135deg, rgba(255,255,255,0.17), rgba(255,255,255,0.07));
                border: 1px solid rgba(255,255,255,0.24);
                box-shadow: 0 10px 22px rgba(0, 0, 0, 0.22);
                backdrop-filter: blur(4px);
            }}

            .kpi-label {{
                margin: 0;
                color: rgba(232, 245, 255, 0.88);
                font-size: 13px;
            }}

            .kpi-value {{
                margin: 4px 0 0 0;
                color: #ffffff;
                font-size: clamp(26px, 2.4vw, 34px);
                font-weight: 700;
                line-height: 1.1;
            }}

            @media (max-width: 960px) {{
                .kpi-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
            }}
        </style>
        <div class="kpi-grid">
            <div class="kpi-card"><p class="kpi-label">Total de LÃ­neas</p><p class="kpi-value">{total_lineas}</p></div>
            <div class="kpi-card"><p class="kpi-label">Gasto Total (USD)</p><p class="kpi-value">${gasto_total:,.2f}</p></div>
            <div class="kpi-card"><p class="kpi-label">Ãreas Diferentes</p><p class="kpi-value">{areas_diff}</p></div>
            <div class="kpi-card"><p class="kpi-label">Departamentos</p><p class="kpi-value">{deptos_diff}</p></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_semaforo_lineas(total_lineas, lineas_libres):
    """Semaforo visual para disponibilidad de lineas."""
    if total_lineas <= 0:
        ratio_libres = 0
    else:
        ratio_libres = lineas_libres / total_lineas

    if ratio_libres >= 0.20:
        estado = "VERDE"
        color = "#2ecc71"
        mensaje = "Disponibilidad saludable de lineas."
    elif ratio_libres >= 0.10:
        estado = "AMARILLO"
        color = "#f1c40f"
        mensaje = "Disponibilidad moderada; conviene monitorear."
    else:
        estado = "ROJO"
        color = "#e74c3c"
        mensaje = "Pocas lineas libres; evaluar nueva contratacion."

    st.markdown(
        f"""
        <div style="display:flex;align-items:center;gap:12px;margin:6px 0 14px 0;
                    padding:10px 12px;border-radius:12px;
                    background:rgba(255,255,255,0.11);border:1px solid rgba(255,255,255,0.22);">
            <div style="width:16px;height:16px;border-radius:50%;background:{color};box-shadow:0 0 14px {color};"></div>
            <div style="color:#f6fbff;font-size:14px;"><b>Semaforo de lineas:</b> {estado} | {mensaje}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def construir_pdf_ejecutivo_dashboard(df, total_lineas, lineas_asignadas, lineas_libres):
    """Genera PDF ejecutivo para el dashboard."""
    if FPDF is None:
        return None

    pdf = FPDF(format='letter')
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 10, "Reporte Ejecutivo - Dashboard Corporativo", ln=True, align='C')
    pdf.set_font("Arial", size=10)
    pdf.cell(0, 8, f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ln=True)
    pdf.ln(2)

    pdf.set_font("Arial", "B", 11)
    pdf.cell(0, 8, "Indicadores Generales", ln=True)
    pdf.set_font("Arial", size=10)
    pdf.cell(0, 7, f"Total de lineas: {total_lineas}", ln=True)
    pdf.cell(0, 7, f"Lineas asignadas: {lineas_asignadas}", ln=True)
    pdf.cell(0, 7, f"Lineas libres/disponibles: {lineas_libres}", ln=True)
    pdf.cell(0, 7, f"Gasto total (USD): ${df['valor_usd'].sum():,.2f}", ln=True)
    pdf.cell(0, 7, f"Promedio por plan (USD): ${df['valor_usd'].mean():,.2f}", ln=True)
    pdf.ln(3)

    pdf.set_font("Arial", "B", 11)
    pdf.cell(0, 8, "Top Areas por Gasto", ln=True)
    pdf.set_font("Arial", size=10)
    gasto_area = df.groupby('area')['valor_usd'].sum().sort_values(ascending=False).head(10)
    for area, gasto in gasto_area.items():
        pdf.cell(0, 7, f"- {area}: ${gasto:,.2f}", ln=True)

    return pdf.output(dest='S').encode('latin-1', errors='replace')


def aplicar_estilo_tabla_profesional():
    """Aplica estilo visual profesional a tablas de Streamlit."""
    st.markdown(
        """
        <style>
            [data-testid="stDataFrame"] {
                border: 1px solid rgba(255, 255, 255, 0.22);
                border-radius: 12px;
                box-shadow: 0 10px 24px rgba(0, 0, 0, 0.20);
                overflow: hidden;
            }

            [data-testid="stDataFrame"] thead tr th {
                background: linear-gradient(180deg, rgba(21, 46, 74, 0.95), rgba(17, 38, 63, 0.95));
                color: #eaf4ff;
                font-weight: 700;
                letter-spacing: 0.2px;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def construir_tabla_planes_profesional(df_tabla):
    """Normaliza y ordena columnas para una vista ejecutiva de la tabla de planes."""
    df_vista = df_tabla.copy()

    if 'nombre_personal' in df_vista.columns:
        asignado = df_vista['nombre_personal'].astype(str).str.strip()
        df_vista['estado_linea'] = asignado.apply(
            lambda x: 'Asignada' if x and x.lower() not in ['none', 'nan'] else 'Libre'
        )
    else:
        df_vista['estado_linea'] = 'Libre'

    if 'operador' not in df_vista.columns:
        df_vista['operador'] = 'TIGO'

    columnas_preferidas = [
        'numero',
        'operador',
        'estado_linea',
        'nombre_personal',
        'area',
        'departamento',
        'perfil_profesional',
        'dispositivo_asignado',
        'marca',
        'modelo',
        'serie_dispositivo',
        'valor_usd',
        'valor_hnl',
        'fecha_creacion',
    ]

    columnas_existentes = [c for c in columnas_preferidas if c in df_vista.columns]
    df_vista = df_vista[columnas_existentes]
    return df_vista

if 'preferencias' not in st.session_state:
    st.session_state.preferencias = {
        "tema": "Claro",
        "idioma": "EspaÃ±ol",
        "fondo_visual": "Impacto",
        "notificaciones": True,
        "columnas_visibles": ["numero", "nombre_personal", "area", "departamento", "valor_usd"]
    }

# ============ SISTEMA DE AUTENTICACIÃ“N ============
def pantalla_login():
    """Pantalla de login"""
    col1, col2, col3 = st.columns([1, 2, 1])
    
    with col2:
        st.markdown("## ðŸ” Sistema de GestiÃ³n de Planes Corporativos")
        st.markdown("---")
        
        usuario = st.text_input("ðŸ‘¤ Usuario:")
        contraseÃ±a = st.text_input("ðŸ”‘ ContraseÃ±a:", type="password")
        
        col_btn1, col_btn2 = st.columns(2)
        
        with col_btn1:
            if st.button("ðŸš€ Iniciar SesiÃ³n", width="stretch"):
                valido, info = gestor_usuarios.validar_usuario(usuario, contraseÃ±a)
                if valido:
                    st.session_state.usuario_actual = usuario
                    st.session_state.rol = info.get("rol", "usuario")
                    st.session_state.puede_editar = bool(info.get("puede_editar", False) or st.session_state.rol in ["administrador", "superadministrador"])
                    st.success("âœ… SesiÃ³n iniciada correctamente")
                    st.rerun()
                else:
                    st.error(f"âŒ {info}")
        
        with col_btn2:
            if st.button("ðŸ‘¤ Crear Cuenta", width="stretch"):
                st.session_state.mostrar_registro = True
                st.rerun()
        
        # Forma de registro
        if 'mostrar_registro' in st.session_state and st.session_state.mostrar_registro:
            st.markdown("---")
            st.subheader("ðŸ“ Crear Nueva Cuenta")
            
            nuevo_usuario = st.text_input("Nuevo usuario:")
            nueva_contraseÃ±a = st.text_input("Nueva contraseÃ±a:", type="password")
            email = st.text_input("Email (opcional):")
            
            if st.button("âœ… Registrarse", width="stretch"):
                if nuevo_usuario and nueva_contraseÃ±a:
                    exito, mensaje = gestor_usuarios.crear_usuario(nuevo_usuario, nueva_contraseÃ±a, "usuario", email)
                    if exito:
                        st.success(f"âœ… {mensaje}")
                        st.session_state.mostrar_registro = False
                        st.rerun()
                    else:
                        st.error(f"âŒ {mensaje}")
                else:
                    st.error("âŒ Usuario y contraseÃ±a son obligatorios")

if not st.session_state.usuario_actual:
    pantalla_login()
    st.stop()
else:
    # ============ APLICACIÃ“N PRINCIPAL ============
    total_lineas_sidebar, lineas_asignadas_sidebar, lineas_disponibles_sidebar = resumen_numeros_corporativos(st.session_state.planes)
    
    # Sidebar con info de usuario
    with st.sidebar:
        st.markdown(f"### ðŸ‘¤ {st.session_state.usuario_actual}")
        st.markdown(f"**Rol:** {st.session_state.rol}")
        st.markdown(f"**Puede editar:** {'SÃ­' if st.session_state.puede_editar else 'No'}")
        st.markdown("---")

        st.markdown(
            """
            <style>
                /* Skin tematico tipo smartphone para el primer expander del sidebar */
                [data-testid="stSidebar"] details[data-testid="stExpander"]:first-of-type {
                    --skin-accent: rgba(86, 207, 255, 0.88);
                    --skin-border: rgba(173, 229, 255, 0.34);
                    border-radius: 36px !important;
                    border: 2px solid var(--skin-border) !important;
                    background:
                        linear-gradient(180deg, rgba(198, 222, 250, 0.72), rgba(152, 185, 222, 0.75)) -3px 116px / 4px 44px no-repeat,
                        linear-gradient(180deg, rgba(198, 222, 250, 0.70), rgba(152, 185, 222, 0.72)) -3px 170px / 4px 28px no-repeat,
                        linear-gradient(180deg, rgba(178, 207, 242, 0.70), rgba(132, 168, 212, 0.72)) calc(100% + 1px) 138px / 4px 56px no-repeat,
                        radial-gradient(circle at 12% 8%, rgba(160, 232, 255, 0.24), rgba(160, 232, 255, 0.0) 42%),
                        radial-gradient(circle at 88% 14%, rgba(126, 204, 255, 0.14), rgba(126, 204, 255, 0.0) 38%),
                        linear-gradient(180deg, rgba(7, 16, 30, 0.97), rgba(5, 10, 20, 0.99)) !important;
                    box-shadow:
                        0 20px 36px rgba(0, 0, 0, 0.45),
                        0 0 0 1px rgba(168, 226, 255, 0.16),
                        inset 0 0 0 1px rgba(255, 255, 255, 0.06),
                        inset 0 -34px 42px rgba(0, 0, 0, 0.24);
                    overflow: visible !important;
                    position: relative;
                    padding: 14px 12px 24px 12px !important;
                    margin-top: 8px !important;
                }

                [data-testid="stSidebar"] details[data-testid="stExpander"]:first-of-type::before {
                    content: "";
                    position: absolute;
                    top: 7px;
                    left: 50%;
                    transform: translateX(-50%);
                    width: 96px;
                    height: 12px;
                    border-radius: 0 0 14px 14px;
                    background:
                        radial-gradient(circle at 79% 50%, rgba(138, 200, 255, 0.92) 0 1.2px, rgba(20, 34, 50, 0.95) 1.3px 3.2px, transparent 3.3px),
                        radial-gradient(circle at 53% 52%, rgba(255, 255, 255, 0.32) 0 0.7px, transparent 0.8px),
                        rgba(3, 7, 14, 0.97);
                    border: 1px solid rgba(255, 255, 255, 0.12);
                    z-index: 5;
                }

                [data-testid="stSidebar"] details[data-testid="stExpander"]:first-of-type::after {
                    content: "";
                    position: absolute;
                    bottom: 8px;
                    left: 50%;
                    transform: translateX(-50%);
                    width: 88px;
                    height: 6px;
                    border-radius: 99px;
                    background: rgba(228, 241, 255, 0.68);
                    box-shadow: 0 0 10px rgba(147, 220, 255, 0.36);
                }

                [data-testid="stSidebar"] details[data-testid="stExpander"]:first-of-type summary {
                    border-radius: 20px !important;
                    background: linear-gradient(180deg, rgba(20, 48, 78, 0.66), rgba(14, 35, 58, 0.72)) !important;
                    border: 1px solid rgba(141, 214, 255, 0.22) !important;
                    margin: 10px 4px 8px 4px !important;
                    box-shadow: inset 0 0 0 1px rgba(214, 241, 255, 0.05);
                    position: relative;
                }

                [data-testid="stSidebar"] details[data-testid="stExpander"]:first-of-type [data-testid="stExpanderDetails"] {
                    border-top: 1px solid rgba(164, 225, 255, 0.18);
                    padding: 12px 4px 0 4px !important;
                    margin-top: 8px;
                }

                [data-testid="stSidebar"] details[data-testid="stExpander"]:first-of-type [data-testid="stMetric"] {
                    border-radius: 14px;
                    padding: 8px 10px;
                    background: linear-gradient(180deg, rgba(15, 30, 49, 0.56), rgba(12, 24, 39, 0.70));
                    border: 1px solid rgba(144, 215, 255, 0.12);
                    margin-bottom: 6px;
                }

                [data-testid="stSidebar"] details[data-testid="stExpander"]:first-of-type button {
                    border-radius: 12px !important;
                    border: 1px solid rgba(149, 219, 255, 0.24) !important;
                    background: linear-gradient(180deg, rgba(28, 58, 91, 0.58), rgba(18, 38, 61, 0.72)) !important;
                }

                [data-testid="stSidebar"] details[data-testid="stExpander"]:first-of-type button:hover {
                    border-color: rgba(166, 231, 255, 0.42) !important;
                    box-shadow: 0 0 0 1px rgba(118, 211, 255, 0.22), 0 8px 16px rgba(0, 0, 0, 0.28);
                }
            </style>
            """,
            unsafe_allow_html=True,
        )

        # Resumen rapido de lineas corporativas en menu izquierdo.
        with st.expander("ðŸ“ž Numeros Corporativos", expanded=True):
            st.metric("Lineas activas", total_lineas_sidebar)
            st.metric("Asignadas", lineas_asignadas_sidebar)
            st.metric("Libres/Disponibles", lineas_disponibles_sidebar)

            if st.button("ðŸ“‹ Ver/Ocultar lista completa", key="btn_toggle_numeros"):
                st.session_state.mostrar_numeros_corporativos = not st.session_state.mostrar_numeros_corporativos

            st.markdown("#### â¬†ï¸ Importar lineas nuevas (sumar)")
            with st.form("form_importar_nuevas_lineas", clear_on_submit=False):
                archivo_nuevas_lineas = st.file_uploader(
                    "CSV o Excel con lineas nuevas",
                    type=["csv", "xlsx", "xls"],
                    key="uploader_nuevas_lineas",
                    help="Columnas sugeridas: numero, nombre_personal, area, departamento, valor_usd",
                )
                st.caption("Se permiten nombres repetidos. La validaciÃ³n de duplicados se realiza por nÃºmero telefÃ³nico.")

                hoja_nuevas_lineas = None
                if archivo_nuevas_lineas is not None and archivo_nuevas_lineas.name.lower().endswith((".xlsx", ".xls")):
                    try:
                        archivo_nuevas_lineas.seek(0)
                        excel_tmp = pd.ExcelFile(archivo_nuevas_lineas)
                        hoja_nuevas_lineas = st.selectbox(
                            "Hoja de Excel",
                            options=excel_tmp.sheet_names,
                            key="hoja_nuevas_lineas",
                        )
                    except Exception as e:
                        st.error(f"No se pudo leer el Excel: {e}")

                if archivo_nuevas_lineas is not None:
                    try:
                        nombre_archivo_preview = archivo_nuevas_lineas.name.lower()
                        archivo_nuevas_lineas.seek(0)
                        if nombre_archivo_preview.endswith(".csv"):
                            df_preview_import = pd.read_csv(archivo_nuevas_lineas)
                        elif nombre_archivo_preview.endswith((".xlsx", ".xls")) and hoja_nuevas_lineas:
                            archivo_nuevas_lineas.seek(0)
                            df_preview_import = pd.read_excel(archivo_nuevas_lineas, sheet_name=hoja_nuevas_lineas)
                        else:
                            df_preview_import = None

                        if df_preview_import is not None:
                            df_preview_import = preparar_dataframe_importacion(df_preview_import)
                            columnas_preview = ", ".join([str(c) for c in df_preview_import.columns[:8]])
                            requeridas_preview = ["numero", "nombre_personal", "area", "departamento", "valor_usd"]
                            requeridas_ok = all(col in df_preview_import.columns for col in requeridas_preview)
                            filas_preview = len(df_preview_import.index)
                            st.caption(f"Archivo listo: {filas_preview} filas detectadas. Columnas: {columnas_preview}")
                            st.caption(f"Columnas requeridas completas: {'Si' if requeridas_ok else 'No'}")
                            if requeridas_ok:
                                resumen_preview = analizar_importacion_lineas(
                                    df_preview_import,
                                    st.session_state.planes,
                                    float(st.session_state.get("tasa_usd_hnl", 24.0)),
                                )
                                st.caption(
                                    f"PrevalidaciÃ³n: nuevas={resumen_preview['nuevas']} | duplicadas={resumen_preview['duplicadas']} | invÃ¡lidas={resumen_preview['invalidas']}"
                                )
                    except Exception as e:
                        st.caption(f"No se pudo generar resumen del archivo: {e}")

                submit_importar_lineas = st.form_submit_button(
                    "âž• Importar lineas nuevas",
                    disabled=not st.session_state.puede_editar,
                    use_container_width=True,
                )

            if submit_importar_lineas:
                procesar_importacion_lineas_nuevas(archivo_nuevas_lineas, hoja_nuevas_lineas)
        
        # API Key de Gemini (solo para superadministrador)
        if st.session_state.rol == "superadministrador":
            api_key = st.text_input("ðŸ”‘ API Key de Gemini:", type="password")
            
            if api_key:
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel('gemini-pro')
        else:
            api_key = None
        
        # Preferencias
        st.markdown("### âš™ï¸ Preferencias")
        tema = st.selectbox("ðŸŽ¨ Tema:", ["Oscuro", "Azul", "Verde", "Rojo", "Claro", "AutomÃ¡tico"])
        apply_theme(tema)
        st.session_state.tema = tema

        fondo_visual = st.selectbox(
            "ðŸ–¼ï¸ Fondo principal:",
            ["Corporativo", "Sutil", "Impacto"],
            index=["Corporativo", "Sutil", "Impacto"].index(st.session_state.preferencias.get("fondo_visual", "Impacto"))
            if st.session_state.preferencias.get("fondo_visual", "Impacto") in ["Corporativo", "Sutil", "Impacto"]
            else 2,
        )
        st.session_state.preferencias["fondo_visual"] = fondo_visual
        apply_main_background(fondo_visual)
        
        idioma = st.selectbox("ðŸŒ Idioma:", ["EspaÃ±ol", "InglÃ©s", "PortuguÃ©s"])
        st.session_state.preferencias["idioma"] = idioma

        # Tasa global de conversiÃ³n USD -> HNL para toda la app
        st.markdown("### ðŸ’± Tasa USD/HNL")
        if 'tasa_usd_hnl' not in st.session_state:
            tasa_inicial, fuente_inicial, fecha_inicial = obtener_tasa_usd_hnl()
            st.session_state.tasa_usd_hnl = tasa_inicial or 24.0
            st.session_state.tasa_fuente = fuente_inicial or "Manual por defecto"
            st.session_state.tasa_actualizada_en = fecha_inicial or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # RevisiÃ³n periÃ³dica para mantener la tasa vigente sin intervenciÃ³n manual.
        ultima_verificacion = st.session_state.get("tasa_ultima_verificacion")
        ahora = datetime.now()
        necesita_verificar = True
        if isinstance(ultima_verificacion, str):
            try:
                fecha_verificacion = datetime.strptime(ultima_verificacion, "%Y-%m-%d %H:%M:%S")
                necesita_verificar = (ahora - fecha_verificacion) >= timedelta(minutes=30)
            except Exception:
                necesita_verificar = True

        if necesita_verificar:
            tasa_auto, fuente_auto, fecha_auto = obtener_tasa_usd_hnl(force_refresh=False, max_horas_cache=6)
            if tasa_auto:
                st.session_state.tasa_usd_hnl = tasa_auto
                st.session_state.tasa_fuente = fuente_auto or st.session_state.get("tasa_fuente", "Fuente externa")
                st.session_state.tasa_actualizada_en = fecha_auto or st.session_state.get(
                    "tasa_actualizada_en", ahora.strftime("%Y-%m-%d %H:%M:%S")
                )
            st.session_state.tasa_ultima_verificacion = ahora.strftime("%Y-%m-%d %H:%M:%S")

        if st.button("ðŸ”„ Actualizar tasa desde Internet"):
            tasa_actualizada, fuente_actualizada, fecha_actualizada = obtener_tasa_usd_hnl(force_refresh=True)
            if tasa_actualizada:
                alerta_variacion = _verificar_alerta_variacion(tasa_actualizada)
                _registrar_tasa_historial(tasa_actualizada, fuente_actualizada or "Fuente externa")
                st.session_state.tasa_usd_hnl = tasa_actualizada
                st.session_state.tasa_fuente = fuente_actualizada or "Fuente externa"
                st.session_state.tasa_actualizada_en = fecha_actualizada or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                st.success(
                    f"Tasa actualizada: {tasa_actualizada:.4f} HNL por USD | Fuente: {st.session_state.tasa_fuente}"
                )
                if alerta_variacion:
                    st.warning(f"âš ï¸ {alerta_variacion}")
            else:
                st.error("No se pudo actualizar desde Internet. Se mantiene la ultima tasa guardada.")

        st.caption(
            f"Fuente: {st.session_state.get('tasa_fuente', 'N/D')} | "
            f"Actualizada: {st.session_state.get('tasa_actualizada_en', 'N/D')}"
        )

        tasa_antes_manual = float(st.session_state.tasa_usd_hnl)

        st.number_input(
            "Tasa manual",
            min_value=0.0,
            value=float(st.session_state.tasa_usd_hnl),
            step=0.01,
            key="sidebar_tasa_usd_hnl"
        )
        st.session_state.tasa_usd_hnl = st.session_state.sidebar_tasa_usd_hnl
        if abs(float(st.session_state.tasa_usd_hnl) - tasa_antes_manual) > 1e-9:
            st.session_state.tasa_fuente = "Manual"
            st.session_state.tasa_actualizada_en = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _guardar_tasa_cache(st.session_state.tasa_usd_hnl, "Manual")
            _registrar_tasa_historial(st.session_state.tasa_usd_hnl, "Manual")
        
        st.markdown("---")
        
        if st.button("ðŸšª Cerrar SesiÃ³n"):
            st.session_state.usuario_actual = None
            st.session_state.rol = None
            st.session_state.puede_editar = False
            st.rerun()
    
    st.markdown(
        """
        <style>
            /* Menu superior: tabs en formato botones tipo tarjeta */
            div[data-baseweb="tab-list"] {
                gap: 10px;
                padding: 4px 0 6px 0;
            }

            div[data-baseweb="tab-list"] button[data-baseweb="tab"] {
                border-radius: 14px !important;
                border: 1px solid rgba(173, 223, 255, 0.34) !important;
                background: linear-gradient(180deg, rgba(58, 130, 194, 0.55), rgba(34, 99, 160, 0.52)) !important;
                color: rgba(231, 245, 255, 0.95) !important;
                font-weight: 600 !important;
                min-height: 44px;
                padding: 0 18px !important;
                box-shadow: 0 8px 18px rgba(6, 28, 52, 0.24);
                transition: transform 0.16s ease, box-shadow 0.16s ease, border-color 0.16s ease;
            }

            div[data-baseweb="tab-list"] button[data-baseweb="tab"]:hover {
                transform: translateY(-1px);
                border-color: rgba(199, 234, 255, 0.58) !important;
                box-shadow: 0 10px 20px rgba(8, 31, 56, 0.32);
            }

            div[data-baseweb="tab-list"] button[data-baseweb="tab"][aria-selected="true"] {
                border-color: rgba(230, 246, 255, 0.86) !important;
                background: linear-gradient(180deg, rgba(100, 177, 241, 0.78), rgba(56, 129, 199, 0.72)) !important;
                color: #ffffff !important;
                box-shadow: 0 0 0 1px rgba(224, 245, 255, 0.48), 0 12px 22px rgba(7, 30, 52, 0.32);
            }

            div[data-baseweb="tab-list"] button[data-baseweb="tab"] p {
                font-size: 20px !important;
                margin: 0 !important;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Tabs principales
    tab1, tab2, tab3, tab4 = st.tabs([
        "ðŸ“Š Dashboard",
        "âž• Agregar Plan",
        "ðŸ“‹ Gestionar Planes",
        "âš™ï¸ ConfiguraciÃ³n"
    ])

    render_hero_principal(st.session_state.usuario_actual, st.session_state.rol)

es_editor = bool(st.session_state.puede_editar or st.session_state.rol in ["administrador", "superadministrador"])
permiso_crear = tiene_permiso("crear")
permiso_editar = tiene_permiso("editar")
permiso_eliminar = tiene_permiso("eliminar")
permiso_importar = tiene_permiso("importar")
permiso_exportar = tiene_permiso("exportar")


def render_bloque_operador_tigo():
    """Muestra branding del operador corporativo actual (TIGO)."""
    col_logo, col_texto = st.columns([1, 7])
    with col_logo:
        st.image(
            "https://upload.wikimedia.org/wikipedia/commons/thumb/5/55/Tigo_logo_2022.svg/320px-Tigo_logo_2022.svg.png",
            width=72,
        )
    with col_texto:
        st.markdown("**Operador de servicio corporativo:** TIGO")
        st.caption("El sistema usa TIGO como operador por defecto en importaciones y nuevos registros.")

# ============ TAB 1: DASHBOARD ============
with tab1:
    st.markdown(
        """<style>
        .kpi-big { font-size: 2.8em; font-weight: 700; color: #00d9ff; text-shadow: 0 0 10px rgba(0, 217, 255, 0.4); }
        .kpi-label { font-size: 0.95em; color: rgba(255,255,255,0.76); font-weight: 500; }
        .kpi-container { background: linear-gradient(135deg, rgba(30,60,120,0.5), rgba(10,35,80,0.5)); border: 1px solid rgba(0,217,255,0.3); border-radius: 12px; padding: 20px; margin: 10px 0; }
        </style>""",
        unsafe_allow_html=True,
    )
    
    if st.session_state.planes:
        df = pd.DataFrame(st.session_state.planes)
        total_lineas = len(df)
        gasto_total = float(df["valor_usd"].sum())
        gasto_promedio = float(df["valor_usd"].mean())
        areas_unicas = int(df["area"].nunique())
        deptos_unicos = int(df["departamento"].nunique())
        lineas_asignadas = len(df[(df["nombre_personal"].notna()) & (df["nombre_personal"] != "")])
        lineas_libres = total_lineas - lineas_asignadas

        st.markdown("### ðŸ“Š Indicadores Principales")
        col_kpi1, col_kpi2, col_kpi3, col_kpi4 = st.columns(4)
        with col_kpi1:
            st.markdown(
                f'<div class="kpi-container"><div class="kpi-label">Total LÃ­neas</div><div class="kpi-big">{total_lineas}</div></div>',
                unsafe_allow_html=True,
            )
        with col_kpi2:
            st.markdown(
                f'<div class="kpi-container"><div class="kpi-label">Gasto Total USD</div><div class="kpi-big">${gasto_total:,.0f}</div></div>',
                unsafe_allow_html=True,
            )
        with col_kpi3:
            st.markdown(
                f'<div class="kpi-container"><div class="kpi-label">LÃ­neas Asignadas</div><div class="kpi-big">{lineas_asignadas}</div></div>',
                unsafe_allow_html=True,
            )
        with col_kpi4:
            st.markdown(
                f'<div class="kpi-container"><div class="kpi-label">LÃ­neas Libres</div><div class="kpi-big">{lineas_libres}</div></div>',
                unsafe_allow_html=True,
            )

        st.markdown("---")

        # GrÃ¡ficos principales con Plotly
        col_chart1, col_chart2 = st.columns(2)

        with col_chart1:
            st.markdown("### ðŸ’° Gasto por Ãrea")
            import plotly.express as px

            gasto_area = df.groupby("area")["valor_usd"].sum().sort_values(ascending=True)
            fig_area = px.bar(
                y=gasto_area.index,
                x=gasto_area.values,
                orientation="h",
                labels={"x": "USD", "y": "Ãrea"},
                color=gasto_area.values,
                color_continuous_scale="Teal",
                title=None,
            )
            fig_area.update_layout(
                height=350,
                margin=dict(l=0, r=0, t=30, b=0),
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                font=dict(color="rgba(255,255,255,0.8)", size=11),
                xaxis=dict(showgrid=False, zeroline=False),
                yaxis=dict(showgrid=False),
            )
            st.plotly_chart(fig_area, use_container_width=True, config={"displayModeBar": False})

        with col_chart2:
            st.markdown("### ðŸ‘¥ DistribuciÃ³n por Departamento")
            lineas_dept = df["departamento"].value_counts().head(8)
            fig_dept = px.pie(
                values=lineas_dept.values,
                names=lineas_dept.index,
                color_discrete_sequence=px.colors.sequential.Blues[::-1],
                title=None,
            )
            fig_dept.update_layout(
                height=350,
                margin=dict(l=0, r=0, t=30, b=0),
                paper_bgcolor="rgba(0,0,0,0)",
                font=dict(color="rgba(255,255,255,0.8)", size=10),
                legend=dict(font=dict(size=9), yanchor="middle", y=0.5),
            )
            st.plotly_chart(fig_dept, use_container_width=True, config={"displayModeBar": False})

        st.markdown("---")

        col_chart3, col_chart4 = st.columns(2)

        with col_chart3:
            st.markdown("### ðŸ“ˆ Tendencia Acumulada de Gasto")
            df_sorted = df.sort_values("fecha_creacion")
            df_sorted["acumulado"] = df_sorted["valor_usd"].cumsum()
            fig_cumsum = px.line(
                x=range(len(df_sorted)),
                y=df_sorted["acumulado"].values,
                labels={"x": "Planes Registrados", "y": "Gasto Acumulado (USD)"},
                title=None,
                markers=True,
            )
            fig_cumsum.update_layout(
                height=320,
                margin=dict(l=0, r=0, t=30, b=0),
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                font=dict(color="rgba(255,255,255,0.8)", size=11),
                hovermode="x unified",
            )
            fig_cumsum.update_traces(line=dict(color="#00d9ff", width=3))
            fig_cumsum.data[0].marker.color = "#00d9ff"
            st.plotly_chart(fig_cumsum, use_container_width=True, config={"displayModeBar": False})

        with col_chart4:
            st.markdown("### ðŸ’µ DistribuciÃ³n de Valores USD")
            fig_scatter = px.scatter(
                df,
                x="area",
                y="valor_usd",
                size="valor_usd",
                color="departamento",
                hover_data=["nombre_personal", "numero"],
                title=None,
                color_discrete_sequence=px.colors.qualitative.Set2,
            )
            fig_scatter.update_layout(
                height=320,
                margin=dict(l=0, r=0, t=30, b=0),
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                font=dict(color="rgba(255,255,255,0.8)", size=10),
                xaxis=dict(showgrid=False),
                yaxis=dict(showgrid=False),
                legend=dict(font=dict(size=8), yanchor="top", y=0.99),
            )
            st.plotly_chart(fig_scatter, use_container_width=True, config={"displayModeBar": False})

        st.markdown("---")
        st.markdown("### ðŸ“Š MÃ©tricas Adicionales")
        metrics_col1, metrics_col2, metrics_col3, metrics_col4 = st.columns(4)
        with metrics_col1:
            st.metric(label="Promedio por LÃ­nea (USD)", value=f"${gasto_promedio:,.2f}")
        with metrics_col2:
            st.metric(label="Ãreas Ãšnicas", value=areas_unicas)
        with metrics_col3:
            st.metric(label="Departamentos", value=deptos_unicos)
        with metrics_col4:
            tasa_ocupacion = (lineas_asignadas / total_lineas * 100) if total_lineas > 0 else 0
            st.metric(label="OcupaciÃ³n (%)", value=f"{tasa_ocupacion:.1f}%")

    else:
        st.info("ðŸ“Œ No hay planes registrados aÃºn. Â¡Comienza agregando uno!")

# ============ TAB 2: AGREGAR PLAN ============
with tab2:
    st.subheader("âž• Agregar Nuevo Plan Corporativo")

    if not permiso_crear:
        st.warning("ðŸ”’ Tu usuario tiene acceso de solo lectura. No puedes crear ni editar planes.")
    
    if not api_key and st.session_state.rol == "superadministrador":
        st.info("ðŸ’¡ Configura tu API Key de Gemini en la barra lateral para habilitar sugerencias inteligentes (no es obligatorio para guardar planes)")

    col1, col2 = st.columns(2)
    
    with col1:
        numero = st.text_input("ðŸ“ž NÃºmero Corporativo", placeholder="Ej: +504-2234-5678")
        st.caption("Formato sugerido: +504-2234-5678")
        st.caption("Se permiten nombres repetidos; el nÃºmero corporativo debe ser Ãºnico.")
        nombre_personal = st.text_input("ðŸ‘¤ Nombre del Personal", placeholder="Ej: Juan PÃ©rez")
        area = st.text_input("ðŸ¢ Ãrea", placeholder="Ej: Ventas")
        operador = st.text_input("ðŸ“¶ Operador", value="TIGO")
        perfil_profesional = st.text_input("ðŸŽ“ Perfil Profesional", placeholder="Ej: Ingeniero de Soporte")
        dispositivo_asignado = st.text_input("ðŸ“± Dispositivo Asignado", placeholder="Ej: Samsung Galaxy S23")
        marca_dispositivo = st.text_input("ðŸ·ï¸ Marca", placeholder="Ej: Samsung")
        modelo_dispositivo = st.text_input("ðŸ†” Modelo", placeholder="Ej: S23")
        serie_dispositivo = st.text_input("ðŸ”¢ Serie del Dispositivo", placeholder="Ej: SN-ABC123456")

    with col2:
        departamento = st.text_input("ðŸ›ï¸ Departamento", placeholder="Ej: Comercial")
        valor_usd = st.number_input("ðŸ’µ Valor del Plan (USD)", min_value=0.0, step=0.01)
        imei1 = st.text_input("ðŸ“³ IMEI 1", placeholder="Ej: 123456789012345")
        imei2 = st.text_input("ðŸ“³ IMEI 2", placeholder="Ej: 543210987654321")
        observaciones = st.text_area("ðŸ“ Observaciones", height=100)
        motivo_cambio_dispositivo = st.text_input("ðŸ“ Motivo de cambio de dispositivo (si aplica)", "")

        valor_lempiras = valor_usd * st.session_state.tasa_usd_hnl
        st.info(f"Total en Lempiras: L {valor_lempiras:,.2f} (Tasa: {st.session_state.tasa_usd_hnl:,.2f})")

    if api_key:
        if st.button("âœ¨ Sugerir InformaciÃ³n con Gemini"):
            if numero and nombre_personal:
                prompt = f"""
                Analiza la siguiente informaciÃ³n de un plan corporativo y proporciona sugerencias:
                - NÃºmero: {numero}
                - Personal: {nombre_personal}
                - Ãrea: {area if area else 'No especificada'}
                - Departamento: {departamento if departamento else 'No especificado'}
                - Valor: ${valor_usd if valor_usd > 0 else 'No especificado'}
                
                Proporciona:
                1. ValidaciÃ³n del nÃºmero telefÃ³nico
                2. Recomendaciones para clasificaciÃ³n
                3. Sugerencias de observaciones relevantes
                4. VerificaciÃ³n de duplicados potenciales
                
                Responde de forma concisa y profesional.
                """

                with st.spinner("ðŸ¤– Gemini estÃ¡ analizando..."):
                    try:
                        response = model.generate_content(prompt)
                        st.info(response.text)
                    except Exception as e:
                        st.error(f"âŒ Error al consultar Gemini: {e}")
                        st.info("AsegÃºrate de tener una API Key vÃ¡lida y conexiÃ³n estable.")
    else:
        st.info("ðŸ’¡ Escribe una API Key para habilitar sugerencias inteligentes y validaciones con Gemini")

    # BotÃ³n guardar
    if st.button("ðŸ’¾ Guardar Plan", width="stretch", type="primary", disabled=not permiso_crear):
        if numero and nombre_personal and area and departamento and perfil_profesional:
            numero_limpio = normalizar_numero_telefonico(numero)
            if not re.match(r"^\+?[0-9\-\s]{7,20}$", numero_limpio):
                st.error("âŒ El nÃºmero corporativo tiene formato invÃ¡lido.")
            existentes = {normalizar_numero_telefonico(p.get('numero', '')) for p in st.session_state.planes}
            if numero_limpio in existentes:
                st.error("âŒ Este nÃºmero corporativo ya existe en el sistema.")
            elif re.match(r"^\+?[0-9\-\s]{7,20}$", numero_limpio):
                dispositivo_historial = []
                if motivo_cambio_dispositivo:
                    dispositivo_historial.append({
                        'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        'usuario': st.session_state.usuario_actual,
                        'motivo': motivo_cambio_dispositivo,
                        'dispositivo_asignado': dispositivo_asignado,
                        'marca': marca_dispositivo,
                        'modelo': modelo_dispositivo,
                        'serie_dispositivo': serie_dispositivo,
                        'imei1': imei1,
                        'imei2': imei2
                    })

                nuevo_plan = {
                    'numero': numero_limpio,
                    'operador': (str(operador).strip() or 'TIGO').upper(),
                    'nombre_personal': nombre_personal,
                    'area': area,
                    'departamento': departamento,
                    'perfil_profesional': perfil_profesional,
                    'valor_usd': valor_usd,
                    'tasa_usd_hnl': st.session_state.tasa_usd_hnl,
                    'valor_hnl': valor_lempiras,
                    'observaciones': observaciones,
                    'dispositivo_asignado': dispositivo_asignado,
                    'marca': marca_dispositivo,
                    'modelo': modelo_dispositivo,
                    'serie_dispositivo': serie_dispositivo,
                    'imei1': imei1,
                    'imei2': imei2,
                    'dispositivo_historial': dispositivo_historial,
                    'fecha_creacion': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                st.session_state.planes.append(nuevo_plan)
                guardar_planes()
                registrar_movimiento("Agregar Plan", f"{numero} - {nombre_personal} - {area} - {departamento} - Dispositivo: {dispositivo_asignado}")
                st.success("âœ… Plan agregado exitosamente!")
                st.balloons()
        else:
            st.error("âŒ Por favor completa los campos obligatorios: NÃºmero, Nombre, Ãrea, Departamento y Perfil Profesional")

# ============ TAB 3: GESTIONAR PLANES ============
with tab3:
    st.subheader("ðŸ“‹ Gestionar Planes Corporativos")
    render_bloque_operador_tigo()

    if not (permiso_editar or permiso_eliminar):
        st.info("ðŸ‘€ Modo solo lectura: puedes visualizar informaciÃ³n, pero no modificarla.")
    
    if st.session_state.planes:
        df = pd.DataFrame(st.session_state.planes)

        # Filtros
        col_filter1, col_filter2, col_filter3, col_filter4 = st.columns(4)
        
        with col_filter1:
            filtro_area = st.multiselect("Filtrar por Ãrea:", options=df['area'].unique())
        
        with col_filter2:
            filtro_dept = st.multiselect("Filtrar por Departamento:", options=df['departamento'].unique())
        
        with col_filter3:
            valor_min = st.number_input("Valor mÃ­nimo (USD):", min_value=0.0, step=0.01)

        with col_filter4:
            filtro_busqueda = st.text_input("Buscar # o nombre:", placeholder="Ej: +504 o Juan")
        
        # Aplicar filtros
        df_filtrado = df.copy()
        
        if filtro_area:
            df_filtrado = df_filtrado[df_filtrado['area'].isin(filtro_area)]
        
        if filtro_dept:
            df_filtrado = df_filtrado[df_filtrado['departamento'].isin(filtro_dept)]
        
        if valor_min > 0:
            df_filtrado = df_filtrado[df_filtrado['valor_usd'] >= valor_min]

        if filtro_busqueda.strip():
            patron = filtro_busqueda.strip().lower()
            df_filtrado = df_filtrado[
                df_filtrado['numero'].astype(str).str.lower().str.contains(patron, na=False)
                | df_filtrado['nombre_personal'].astype(str).str.lower().str.contains(patron, na=False)
            ]

        aplicar_estilo_tabla_profesional()
        df_vista_planes = construir_tabla_planes_profesional(df_filtrado)
        st.dataframe(
            df_vista_planes,
            width="stretch",
            hide_index=True,
            height=430,
            column_config={
                "numero": st.column_config.TextColumn("NÃºmero", width="medium"),
                "operador": st.column_config.TextColumn("Operador", width="small"),
                "estado_linea": st.column_config.TextColumn("Estado", width="small"),
                "nombre_personal": st.column_config.TextColumn("Asignado a", width="medium"),
                "area": st.column_config.TextColumn("Ãrea", width="medium"),
                "departamento": st.column_config.TextColumn("Departamento", width="medium"),
                "perfil_profesional": st.column_config.TextColumn("Perfil", width="large"),
                "dispositivo_asignado": st.column_config.TextColumn("Dispositivo", width="medium"),
                "marca": st.column_config.TextColumn("Marca", width="small"),
                "modelo": st.column_config.TextColumn("Modelo", width="small"),
                "serie_dispositivo": st.column_config.TextColumn("Serie", width="medium"),
                "valor_usd": st.column_config.NumberColumn("Valor USD", format="$ %.2f"),
                "valor_hnl": st.column_config.NumberColumn("Valor HNL", format="L %.2f"),
                "fecha_creacion": st.column_config.TextColumn("Fecha", width="medium"),
            },
        )
        
        st.markdown("---")
        
        # Opciones de gestiÃ³n
        col_gestiÃ³n1, col_gestiÃ³n2, col_gestiÃ³n3 = st.columns(3)
        
        with col_gestiÃ³n1:
            st.markdown("**ðŸ“¥ Descargar Reporte:**")
            formato_descarga = st.selectbox(
                "Formato de descarga:",
                ["CSV", "PDF", "Excel"],
                label_visibility="collapsed",
                key="formato_descarga_gestionar"
            )
            
            n_filtrados = len(df_filtrado)
            n_total = len(df)
            etiqueta_filtro = (
                f"({n_filtrados} de {n_total} registros â€” filtrados)"
                if n_filtrados < n_total
                else f"({n_total} registros â€” todos)"
            )
            if st.button(f"ðŸ“¥ Descargar {etiqueta_filtro}", disabled=not permiso_exportar):
                from io import BytesIO as _BytesIO
                # Siempre exportar el conjunto filtrado visible
                _df_export = df_filtrado.copy()
                _sufijo = datetime.now().strftime('%Y%m%d')
                _nombre_base = f"planes_corporativos_{_sufijo}"

                if formato_descarga == "CSV":
                    _csv_data = _df_export.to_csv(index=False).encode("utf-8")
                    st.download_button(
                        label="Descargar CSV",
                        data=_csv_data,
                        file_name=f"{_nombre_base}.csv",
                        mime="text/csv",
                        key="btn_csv_gestionar",
                    )

                elif formato_descarga == "Excel":
                    _excel_buffer = _BytesIO()
                    _excel_ok = False
                    for _engine in ("xlsxwriter", "openpyxl"):
                        try:
                            with pd.ExcelWriter(_excel_buffer, engine=_engine) as _writer:
                                _df_export.to_excel(_writer, index=False, sheet_name="Planes")
                            _excel_ok = True
                            break
                        except Exception:
                            _excel_buffer = _BytesIO()
                    if _excel_ok:
                        _excel_buffer.seek(0)
                        st.download_button(
                            label="Descargar Excel",
                            data=_excel_buffer,
                            file_name=f"{_nombre_base}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            key="btn_excel_gestionar",
                        )
                    else:
                        st.warning("âš ï¸ Instala xlsxwriter o openpyxl: `pip install openpyxl`")

                elif formato_descarga == "PDF":
                    if FPDF is not None:
                        try:
                            _pdf = FPDF(format="letter")
                            _pdf.set_auto_page_break(auto=True, margin=15)
                            _pdf.add_page()
                            _pdf.set_font("Arial", "B", 13)
                            _pdf.cell(
                                0, 10,
                                _texto_seguro_pdf(f"Reporte de Planes Corporativos - {_sufijo}"),
                                ln=True, align="C",
                            )
                            _pdf.set_font("Arial", size=9)
                            _pdf.cell(
                                0, 7,
                                _texto_seguro_pdf(
                                    f"Registros exportados: {n_filtrados}"
                                    + (" (resultado de filtro aplicado)" if n_filtrados < n_total else "")
                                ),
                                ln=True, align="C",
                            )
                            _pdf.ln(3)
                            _pdf.set_font("Arial", size=9)
                            for _i, (_idx, _row) in enumerate(_df_export.iterrows(), start=1):
                                try:
                                    _line = _texto_seguro_pdf(
                                        f"{_i}. {_row.get('numero','')} | {_row.get('nombre_personal','')} | "
                                        f"{_row.get('area','')} | {_row.get('departamento','')} | "
                                        f"USD {float(_row.get('valor_usd', 0)):,.2f} | "
                                        f"HNL {float(_row.get('valor_hnl', 0)):,.2f}"
                                    )
                                    _pdf.multi_cell(180, 7, _line)
                                    _obs = _texto_seguro_pdf(_row.get("observaciones", ""))
                                    if _obs:
                                        _pdf.multi_cell(180, 6, f"Obs: {_obs}")
                                    _pdf.ln(1)
                                except Exception:
                                    pass
                            try:
                                _pdf_bytes = _pdf.output()
                            except TypeError:
                                _pdf_bytes = _pdf.output(dest="S").encode("latin-1")
                            st.download_button(
                                label="Descargar PDF",
                                data=_pdf_bytes,
                                file_name=f"{_nombre_base}.pdf",
                                mime="application/pdf",
                                key="btn_pdf_gestionar",
                            )
                        except Exception as _pdf_error:
                            st.warning(f"No se pudo preparar el PDF: {_pdf_error}")
                    else:
                        st.warning("âš ï¸ Instala fpdf2: `pip install fpdf2`")
        
        with col_gestiÃ³n2:
            if st.button("ðŸ—‘ï¸ Limpiar todos los datos", disabled=not permiso_eliminar):
                if st.confirm("Â¿EstÃ¡s seguro de que deseas eliminar todos los planes?"):
                    st.session_state.planes = []
                    guardar_planes()
                    st.rerun()
        
        with col_gestiÃ³n3:
            if st.button("ðŸ”„ Actualizar vista"):
                st.rerun()

        st.markdown("---")
        st.subheader("âœï¸ EdiciÃ³n de Planes")

        # EdiciÃ³n rÃ¡pida de un plan existente (se muestra debajo de la tabla)
        indice_seleccionado = st.selectbox(
            "Selecciona un plan para editar:",
            options=list(range(len(st.session_state.planes))),
            format_func=lambda i: f"{st.session_state.planes[i]['numero']} - {st.session_state.planes[i]['nombre_personal']} ({st.session_state.planes[i]['area']})",
            key="selector_plan_editar_tab3",
        )

        plan_sel = st.session_state.planes[indice_seleccionado]

        with st.form(key='form_editar_plan'):
            numero_edit = st.text_input("ðŸ“ž NÃºmero Corporativo", value=plan_sel.get('numero', ''))
            operador_edit = st.text_input("ðŸ“¶ Operador", value=plan_sel.get('operador', 'TIGO'))
            nombre_edit = st.text_input("ðŸ‘¤ Nombre del Personal", value=plan_sel.get('nombre_personal', ''))
            area_edit = st.text_input("ðŸ¢ Ãrea", value=plan_sel.get('area', ''))
            departamento_edit = st.text_input("ðŸ›ï¸ Departamento", value=plan_sel.get('departamento', ''))
            perfil_edit = st.text_input("ðŸŽ“ Perfil Profesional", value=plan_sel.get('perfil_profesional', ''))
            valor_usd_edit = st.number_input("ðŸ’µ Valor del Plan (USD)", min_value=0.0, step=0.01, value=float(plan_sel.get('valor_usd', 0)))
            observaciones_edit = st.text_area("ðŸ“ Observaciones", value=plan_sel.get('observaciones', ''), height=100)
            tasa_manual_edit = st.number_input("ðŸ’± Tasa USD a Lempira (HNL)", min_value=0.0, value=float(plan_sel.get('tasa_usd_hnl', 24.0)), step=0.01)

            dispositivo_asignado_edit = st.text_input("ðŸ“± Dispositivo Asignado", value=plan_sel.get('dispositivo_asignado', ''))
            marca_dispositivo_edit = st.text_input("ðŸ·ï¸ Marca", value=plan_sel.get('marca', ''))
            modelo_dispositivo_edit = st.text_input("ðŸ†” Modelo", value=plan_sel.get('modelo', ''))
            serie_dispositivo_edit = st.text_input("ðŸ”¢ Serie del Dispositivo", value=plan_sel.get('serie_dispositivo', ''))
            imei1_edit = st.text_input("ðŸ“³ IMEI 1", value=plan_sel.get('imei1', ''))
            imei2_edit = st.text_input("ðŸ“³ IMEI 2", value=plan_sel.get('imei2', ''))
            motivo_cambio_dispositivo_edit = st.text_input("ðŸ“ Motivo de cambio de dispositivo (si aplica)", "")

            guardar_edicion = st.form_submit_button("ðŸ’¾ Guardar cambios", disabled=not permiso_editar)
            if guardar_edicion:
                numero_edit = normalizar_numero_telefonico(numero_edit)
                if not numero_edit:
                    st.error("âŒ El nÃºmero corporativo es obligatorio.")
                    st.stop()

                existentes_otros = {
                    normalizar_numero_telefonico(p.get('numero', ''))
                    for idx, p in enumerate(st.session_state.planes)
                    if idx != indice_seleccionado
                }
                if numero_edit in existentes_otros:
                    st.error("âŒ Ya existe otro plan con este nÃºmero corporativo.")
                    st.stop()

                dispositivo_historial_actual = plan_sel.get('dispositivo_historial', [])

                if (dispositivo_asignado_edit != plan_sel.get('dispositivo_asignado', '') or
                    marca_dispositivo_edit != plan_sel.get('marca', '') or
                    modelo_dispositivo_edit != plan_sel.get('modelo', '') or
                    serie_dispositivo_edit != plan_sel.get('serie_dispositivo', '') or
                    imei1_edit != plan_sel.get('imei1', '') or
                    imei2_edit != plan_sel.get('imei2', '')):
                    if motivo_cambio_dispositivo_edit:
                        cambio_entry = {
                            'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            'usuario': st.session_state.usuario_actual,
                            'motivo': motivo_cambio_dispositivo_edit,
                            'dispositivo_asignado': dispositivo_asignado_edit,
                            'marca': marca_dispositivo_edit,
                            'modelo': modelo_dispositivo_edit,
                            'serie_dispositivo': serie_dispositivo_edit,
                            'imei1': imei1_edit,
                            'imei2': imei2_edit
                        }
                        dispositivo_historial_actual.append(cambio_entry)
                        guardar_planes()
                        registrar_movimiento("Cambio de Dispositivo", f"{numero_edit} - {nombre_edit} - motivo: {motivo_cambio_dispositivo_edit}")
                        st.info("â„¹ï¸ Historial de dispositivo actualizado.")
                    else:
                        st.warning("âš ï¸ Has modificado informaciÃ³n de dispositivo, por favor proporciona un motivo de cambio para registrar el historial.")

                st.session_state.planes[indice_seleccionado] = {
                    'numero': numero_edit,
                    'operador': (str(operador_edit).strip() or 'TIGO').upper(),
                    'nombre_personal': nombre_edit,
                    'area': area_edit,
                    'departamento': departamento_edit,
                    'perfil_profesional': perfil_edit,
                    'valor_usd': valor_usd_edit,
                    'tasa_usd_hnl': tasa_manual_edit,
                    'valor_hnl': valor_usd_edit * tasa_manual_edit,
                    'observaciones': observaciones_edit,
                    'dispositivo_asignado': dispositivo_asignado_edit,
                    'marca': marca_dispositivo_edit,
                    'modelo': modelo_dispositivo_edit,
                    'serie_dispositivo': serie_dispositivo_edit,
                    'imei1': imei1_edit,
                    'imei2': imei2_edit,
                    'dispositivo_historial': dispositivo_historial_actual,
                    'fecha_creacion': plan_sel.get('fecha_creacion', datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                }
                guardar_planes()
                registrar_movimiento("Editar Plan", f"{numero_edit} - {nombre_edit} - {area_edit} - {departamento_edit}")
                st.success("âœ… Plan actualizado correctamente")
                try:
                    st.rerun()
                except Exception:
                    pass

        if st.button("ðŸ—‘ï¸ Eliminar plan seleccionado", disabled=not permiso_eliminar, key="btn_eliminar_plan_tab3"):
            eliminado = st.session_state.planes.pop(indice_seleccionado)
            guardar_planes()
            registrar_movimiento("Eliminar Plan", f"{eliminado.get('numero')} - {eliminado.get('nombre_personal')} - {eliminado.get('area')} - {eliminado.get('departamento')}")
            st.success("âœ… Plan eliminado")
            try:
                st.rerun()
            except Exception:
                pass

        st.markdown("---")
        st.subheader("ðŸ“Œ Historial de Dispositivo Asignado")
        historial = plan_sel.get('dispositivo_historial', [])
        if historial:
            for evento in sorted(historial, key=lambda x: x.get('fecha', ''), reverse=True):
                st.write(f"- {evento.get('fecha')} / {evento.get('usuario')} / Motivo: {evento.get('motivo')}")
                st.write(f"  Dispositivo: {evento.get('dispositivo_asignado')} | Marca: {evento.get('marca')} | Modelo: {evento.get('modelo')} | Serie: {evento.get('serie_dispositivo')} | IMEI1: {evento.get('imei1')} | IMEI2: {evento.get('imei2')}")
        else:
            st.info("AÃºn no hay cambios de dispositivo registrados para este plan.")
    
    else:
        st.info("ðŸ“Œ No hay planes registrados aÃºn")

# ============ TAB 4: CONFIGURACIÃ“N ============
with tab4:
    st.subheader("âš™ï¸ ConfiguraciÃ³n y Datos")

    # ---- Recalcular planes con tasa actual ----
    st.markdown("### ðŸ’± Recalcular Planes con Tasa Actual")
    tasa_vigente_tab4 = float(st.session_state.get("tasa_usd_hnl", 24.0))
    st.info(
        f"Tasa vigente: **{tasa_vigente_tab4:.4f} HNL/USD** | "
        f"Fuente: {st.session_state.get('tasa_fuente', 'N/D')} | "
        f"Actualizada: {st.session_state.get('tasa_actualizada_en', 'N/D')}"
    )
    if st.button(
        f"ðŸ” Recalcular valor HNL de todos los planes ({len(st.session_state.planes)} registros)",
        disabled=not st.session_state.get("puede_editar", False),
    ):
        if st.session_state.planes:
            actualizados = 0
            for plan in st.session_state.planes:
                try:
                    plan["tasa_usd_hnl"] = tasa_vigente_tab4
                    plan["valor_hnl"] = round(float(plan.get("valor_usd", 0)) * tasa_vigente_tab4, 2)
                    actualizados += 1
                except Exception:
                    pass
            guardar_planes()
            st.success(
                f"âœ… {actualizados} planes recalculados. "
                f"Valor HNL = USD x {tasa_vigente_tab4:.4f}"
            )
        else:
            st.warning("No hay planes registrados para recalcular.")

    st.markdown("---")

    # ---- Historial de tasa USD/HNL ----
    st.markdown("### ðŸ“ˆ Historial de Tasa USD/HNL")
    if os.path.exists(ARCHIVO_TASA_HISTORIAL):
        try:
            with open(ARCHIVO_TASA_HISTORIAL, "r", encoding="utf-8") as _fh:
                _hist = json.load(_fh)
            if _hist:
                _df_hist = pd.DataFrame(_hist)
                _df_hist["datetime"] = pd.to_datetime(
                    _df_hist["fecha"] + " " + _df_hist["hora"], errors="coerce"
                )
                _df_hist = _df_hist.sort_values("datetime", ascending=False).reset_index(drop=True)
                _df_hist["variacion"] = (
                    _df_hist["tasa"].diff(-1)
                    .apply(lambda x: f"+{x:.4f}" if isinstance(x, float) and x > 0 else (f"{x:.4f}" if isinstance(x, float) else ""))
                )
                col_hist_graf, col_hist_tabla = st.columns([3, 2])
                with col_hist_graf:
                    import copy as _copy
                    _df_graf = _df_hist[["datetime", "tasa"]].dropna().sort_values("datetime")
                    if not _df_graf.empty:
                        st.line_chart(_df_graf.set_index("datetime")["tasa"])
                with col_hist_tabla:
                    st.dataframe(
                        _df_hist[["fecha", "hora", "tasa", "fuente", "variacion"]].head(30),
                        use_container_width=True,
                        hide_index=True,
                    )
                # Boton para descargar historial CSV
                csv_hist = _df_hist[["fecha", "hora", "tasa", "fuente"]].to_csv(index=False)
                st.download_button(
                    label="ðŸ“¥ Descargar historial CSV",
                    data=csv_hist.encode("utf-8"),
                    file_name=f"historial_tasa_usd_hnl_{datetime.now().strftime('%Y%m%d')}.csv",
                    mime="text/csv",
                )
            else:
                st.caption("Sin registros en el historial aun.")
        except Exception as _e:
            st.caption(f"No se pudo leer el historial: {_e}")
    else:
        st.caption("El historial se creara automaticamente al actualizar la tasa.")

    st.markdown("---")
    
    col_config1, col_config2 = st.columns(2)
    
    with col_config1:
        st.info("**API de Gemini:** Configurada âœ…" if api_key else "**API de Gemini:** No configurada âš ï¸")
        st.info(f"**Total de Planes:** {len(st.session_state.planes)}")
    
    with col_config2:
        if st.session_state.planes:
            df_temp = pd.DataFrame(st.session_state.planes)
            st.info(f"**InversiÃ³n Total:** ${df_temp['valor_usd'].sum():,.2f}")
            st.info(f"**Ãšltima ActualizaciÃ³n:** {df_temp['fecha_creacion'].iloc[-1]}")

    st.markdown("### ðŸ§¹ Mantenimiento de NÃºmeros")
    st.caption("Normaliza formato (+504...) y elimina duplicados exactos por nÃºmero.")
    if st.button("ðŸ§¹ Normalizar y deduplicar nÃºmeros", disabled=not permiso_editar):
        antes = len(st.session_state.planes)
        planes_limpios, cambios_numero, duplicados_eliminados = normalizar_y_deduplicar_planes(st.session_state.planes)
        st.session_state.planes = planes_limpios
        guardar_planes()
        registrar_movimiento(
            "Mantenimiento NÃºmeros",
            f"cambios_formato={cambios_numero}; duplicados_eliminados={duplicados_eliminados}; total_antes={antes}; total_despues={len(planes_limpios)}",
        )
        st.success(
            f"Mantenimiento completado. Formatos corregidos: {cambios_numero}. Duplicados eliminados: {duplicados_eliminados}."
        )
        st.rerun()
    
    st.markdown("---")
    
    st.subheader("ðŸ“¤ Importar/Exportar Datos")
    
    col_import, col_export = st.columns(2)
    
    with col_import:
        st.markdown("### ðŸ“¥ Importar datos masivos (CSV/Excel)")
        archivo_datos = st.file_uploader(
            "Selecciona un archivo de hoja de calculo",
            type=['csv', 'xlsx', 'xls'],
            help="Formatos permitidos: CSV, Excel (.xlsx, .xls)",
        )

        hoja_excel = None
        df_preview = None
        if archivo_datos is not None:
            nombre_archivo = archivo_datos.name.lower()
            if nombre_archivo.endswith('.xlsx') or nombre_archivo.endswith('.xls'):
                try:
                    excel_file = pd.ExcelFile(archivo_datos)
                    hoja_excel = st.selectbox(
                        "Selecciona la hoja del Excel",
                        options=excel_file.sheet_names,
                        key="hoja_excel_importacion",
                    )
                    st.caption(f"Hojas detectadas: {', '.join(excel_file.sheet_names)}")
                except ImportError:
                    st.error("âŒ Falta una libreria para leer Excel. Instala openpyxl y vuelve a intentar.")
                except Exception as e:
                    st.error(f"âŒ No pude leer las hojas del Excel: {e}")

            # Vista previa antes de importar (10 filas)
            try:
                archivo_datos.seek(0)
                if nombre_archivo.endswith('.csv'):
                    df_preview = pd.read_csv(archivo_datos).head(10)
                elif (nombre_archivo.endswith('.xlsx') or nombre_archivo.endswith('.xls')) and hoja_excel:
                    df_preview = pd.read_excel(archivo_datos, sheet_name=hoja_excel).head(10)

                if df_preview is not None:
                    df_preview = preparar_dataframe_importacion(df_preview)
                    st.markdown("#### ðŸ‘€ Vista previa (primeras 10 filas)")
                    st.dataframe(df_preview, width="stretch", hide_index=True)
                    st.caption(f"Columnas detectadas: {', '.join([str(c) for c in df_preview.columns])}")
            except Exception as e:
                st.warning(f"No se pudo mostrar vista previa: {e}")

        if archivo_datos is not None:
            if st.button("ðŸ”„ Cargar datos masivos", disabled=not permiso_importar):
                try:
                    nombre_archivo = archivo_datos.name.lower()
                    if nombre_archivo.endswith('.csv'):
                        df_importado = pd.read_csv(archivo_datos)
                        tipo_fuente = "CSV"
                    elif nombre_archivo.endswith('.xlsx') or nombre_archivo.endswith('.xls'):
                        if not hoja_excel:
                            st.error("âŒ Selecciona una hoja del Excel para continuar.")
                            st.stop()
                        archivo_datos.seek(0)
                        df_importado = pd.read_excel(archivo_datos, sheet_name=hoja_excel)
                        tipo_fuente = f"Excel ({hoja_excel})"
                    else:
                        st.error("âŒ Formato no soportado. Usa CSV, XLSX o XLS.")
                        st.stop()

                    # Normaliza nombres de columnas para soportar variaciones de encabezados.
                    df_importado = preparar_dataframe_importacion(df_importado)

                    columnas_requeridas = ['numero', 'nombre_personal', 'area', 'departamento', 'valor_usd']
                    if not all(col in df_importado.columns for col in columnas_requeridas):
                        st.error(f"âŒ El archivo debe tener las columnas: {', '.join(columnas_requeridas)}")
                    else:
                        df_importado['numero'] = df_importado['numero'].apply(normalizar_numero_telefonico)
                        if 'operador' not in df_importado.columns:
                            df_importado['operador'] = 'TIGO'
                        else:
                            df_importado['operador'] = (
                                df_importado['operador']
                                .fillna('TIGO')
                                .astype(str)
                                .str.strip()
                                .replace('', 'TIGO')
                                .str.upper()
                            )
                        df_importado = df_importado[df_importado['numero'].astype(str).str.strip() != ""].copy()
                        df_importado['valor_usd'] = pd.to_numeric(df_importado['valor_usd'], errors='coerce').fillna(0.0)

                        if 'tasa_usd_hnl' not in df_importado.columns:
                            df_importado['tasa_usd_hnl'] = float(st.session_state.tasa_usd_hnl)
                        else:
                            df_importado['tasa_usd_hnl'] = pd.to_numeric(df_importado['tasa_usd_hnl'], errors='coerce').fillna(float(st.session_state.tasa_usd_hnl))

                        if 'valor_hnl' not in df_importado.columns:
                            df_importado['valor_hnl'] = df_importado['valor_usd'] * df_importado['tasa_usd_hnl']
                        else:
                            df_importado['valor_hnl'] = pd.to_numeric(df_importado['valor_hnl'], errors='coerce').fillna(df_importado['valor_usd'] * df_importado['tasa_usd_hnl'])

                        if 'fecha_creacion' not in df_importado.columns:
                            fecha_actual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            df_importado['fecha_creacion'] = fecha_actual

                        planes_limpios, cambios_numero, duplicados_eliminados = normalizar_y_deduplicar_planes(df_importado.to_dict('records'))
                        st.session_state.planes = planes_limpios
                        guardar_planes()
                        registrar_movimiento(
                            "Importar datos",
                            f"{len(st.session_state.planes)} planes importados desde {tipo_fuente}; formato_corregido={cambios_numero}; duplicados_eliminados={duplicados_eliminados}",
                        )
                        st.success(
                            f"âœ… {len(st.session_state.planes)} planes importados desde {tipo_fuente}. "
                            f"Formato corregido: {cambios_numero}. Duplicados eliminados: {duplicados_eliminados}."
                        )
                        st.rerun()
                except ImportError:
                    st.error("âŒ Falta una libreria para leer Excel. Instala openpyxl y vuelve a intentar.")
                except Exception as e:
                    st.error(f"âŒ Error al cargar el archivo: {str(e)}")
    
    with col_export:
        st.markdown("### ðŸ“¤ Exportar Datos")
        if st.session_state.planes:
            df = pd.DataFrame(st.session_state.planes)

            # Exportar como CSV
            csv_data = df.to_csv(index=False)
            st.download_button(
                label="ðŸ“„ Descargar como CSV",
                data=csv_data,
                file_name=f"planes_corporativos_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
                disabled=not permiso_exportar,
            )

            # Exportar como Excel
            from io import BytesIO
            excel_buffer = BytesIO()
            try:
                with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False, sheet_name='Planes')

                    # Hoja resumen por area y departamento.
                    resumen_area = df.groupby('area', dropna=False)['valor_usd'].sum().reset_index().sort_values('valor_usd', ascending=False)
                    resumen_area.to_excel(writer, index=False, sheet_name='Resumen_Area')

                    resumen_depto = df.groupby('departamento', dropna=False).size().reset_index(name='cantidad_lineas').sort_values('cantidad_lineas', ascending=False)
                    resumen_depto.to_excel(writer, index=False, sheet_name='Resumen_Departamento')
            except ModuleNotFoundError:
                try:
                    with pd.ExcelWriter(excel_buffer, engine='xlsxwriter') as writer:
                        df.to_excel(writer, index=False, sheet_name='Planes')

                        resumen_area = df.groupby('area', dropna=False)['valor_usd'].sum().reset_index().sort_values('valor_usd', ascending=False)
                        resumen_area.to_excel(writer, index=False, sheet_name='Resumen_Area')

                        resumen_depto = df.groupby('departamento', dropna=False).size().reset_index(name='cantidad_lineas').sort_values('cantidad_lineas', ascending=False)
                        resumen_depto.to_excel(writer, index=False, sheet_name='Resumen_Departamento')
                except ModuleNotFoundError:
                    excel_buffer = None
            
            if excel_buffer is not None:
                excel_buffer.seek(0)
                st.download_button(
                    label="ðŸ“Š Descargar como Excel",
                    data=excel_buffer,
                    file_name=f"planes_corporativos_{datetime.now().strftime('%Y%m%d')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    disabled=not permiso_exportar,
                )
            else:
                st.warning("âš ï¸ No se encontrÃ³ biblioteca para generar Excel (xlsxwriter/openpyxl). Instala una con pip para habilitar esta opciÃ³n.")

            # Exportar como JSON
            json_data = json.dumps(st.session_state.planes, indent=2, ensure_ascii=False, default=str)
            st.download_button(
                label="ðŸ“‹ Descargar como JSON",
                data=json_data,
                file_name=f"planes_corporativos_{datetime.now().strftime('%Y%m%d')}.json",
                mime="application/json",
                disabled=not permiso_exportar,
            )

            # Exportar como PDF
            if FPDF is not None:
                try:
                    pdf = FPDF(format='letter')
                    pdf.set_auto_page_break(auto=True, margin=15)
                    pdf.add_page()
                    pdf.set_font("Arial", size=12)
                    pdf.cell(0, 10, _texto_seguro_pdf("Reporte de Planes Corporativos"), ln=True, align='C')
                    pdf.cell(0, 10, "", ln=True)

                    for idx, row in df.iterrows():
                        line = _texto_seguro_pdf(
                            f"{idx+1}. {row['numero']} | {row['nombre_personal']} | {row['perfil_profesional']} | "
                            f"{row['area']} | {row['departamento']} | USD {row['valor_usd']:,.2f} | "
                            f"HNL {row.get('valor_hnl', 0):,.2f} | Tasa {row.get('tasa_usd_hnl', 0):,.2f}"
                        )
                        pdf.multi_cell(180, 8, line)
                        observ = _texto_seguro_pdf(row.get('observaciones', ''))
                        if observ:
                            pdf.multi_cell(180, 8, f"Observaciones: {observ}")
                        pdf.ln(1)

                    try:
                        pdf_bytes = pdf.output()
                    except TypeError:
                        pdf_bytes = pdf.output(dest='S').encode('latin-1')

                    st.download_button(
                        label="ðŸ“• Descargar como PDF",
                        data=pdf_bytes,
                        file_name=f"planes_corporativos_{datetime.now().strftime('%Y%m%d')}.pdf",
                        mime="application/pdf",
                        disabled=not permiso_exportar,
                    )
                except Exception as pdf_error:
                    st.warning(f"No se pudo generar el PDF: {pdf_error}")
            else:
                st.warning("âš ï¸ Instala `fpdf` (pip install fpdf) para habilitar exportaciÃ³n PDF")
        else:
            st.info("No hay datos para exportar")
    
    st.markdown("---")
    st.subheader("ðŸ‘¥ GestiÃ³n de Usuarios")

    if st.session_state.rol in ["superadministrador", "administrador"]:
        st.info("Crea usuarios, define rol y decide si cada cuenta puede editar o solo visualizar.")
        usuarios = gestor_usuarios.obtener_usuarios()

        with st.form(key='form_usuario'):
            st.markdown("### âž• Crear Usuario")
            usuario_nuevo = st.text_input("Usuario:")
            contrasena_nuevo = st.text_input("ContraseÃ±a:", type='password')
            email_nuevo = st.text_input("Email (opcional):")
            puede_editar_nuevo = st.checkbox("Puede editar datos", value=True)

            if st.session_state.rol == "superadministrador":
                rol_nuevo = st.selectbox("Rol:", ["usuario", "administrador", "superadministrador"])
            else:
                rol_nuevo = "usuario"
                st.info("Como administrador, solo puedes crear usuarios con rol usuario.")

            crear = st.form_submit_button("ðŸ” Crear usuario")
            if crear:
                if not usuario_nuevo or not contrasena_nuevo:
                    st.error("Usuario y contraseÃ±a son obligatorios")
                elif st.session_state.rol != "superadministrador" and rol_nuevo != "usuario":
                    st.error("Solo superadministrador puede crear administradores o superadministradores")
                else:
                    exito, msg = gestor_usuarios.crear_usuario(
                        usuario_nuevo,
                        contrasena_nuevo,
                        rol_nuevo,
                        email_nuevo,
                        puede_editar_nuevo,
                    )
                    if exito:
                        registrar_movimiento(
                            "Crear Usuario",
                            f"{usuario_nuevo} - rol={rol_nuevo} - puede_editar={puede_editar_nuevo}",
                        )
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)

        st.markdown("---")
        st.subheader("Usuarios registrados")
        tabla = []
        for u in usuarios:
            data_u = gestor_usuarios.usuarios.get(u, {})
            permisos_u = data_u.get("permisos", {})
            tabla.append({
                "Usuario": u,
                "Rol": data_u.get("rol", "usuario"),
                "Puede editar": "SÃ­" if data_u.get("puede_editar", False) else "No",
                "Crear": "SÃ­" if permisos_u.get("crear", False) else "No",
                "Editar": "SÃ­" if permisos_u.get("editar", False) else "No",
                "Eliminar": "SÃ­" if permisos_u.get("eliminar", False) else "No",
                "Importar": "SÃ­" if permisos_u.get("importar", False) else "No",
                "Exportar": "SÃ­" if permisos_u.get("exportar", False) else "No",
                "Email": data_u.get("email", ""),
            })
        st.dataframe(pd.DataFrame(tabla), width="stretch", hide_index=True)

        st.markdown("### âœï¸ Editar Permisos / ContraseÃ±a")
        usuario_obj = st.selectbox("Selecciona un usuario", options=usuarios, key="usuario_objetivo")
        data_u = gestor_usuarios.usuarios.get(usuario_obj, {})
        rol_actual_obj = data_u.get("rol", "usuario")
        puede_gestionar_obj = st.session_state.rol == "superadministrador" or rol_actual_obj == "usuario"

        if not puede_gestionar_obj:
            st.warning("Solo superadministrador puede editar cuentas administrador/superadministrador.")
        else:
            with st.form(key="form_editar_usuario"):
                nueva_password = st.text_input("Nueva contraseÃ±a (opcional)", type="password")
                nuevo_email = st.text_input("Email", value=data_u.get("email", ""))
                puede_editar_obj = st.checkbox("Puede editar datos", value=bool(data_u.get("puede_editar", False)))

                permisos_actuales = data_u.get("permisos", {})
                st.markdown("Permisos finos")
                colp1, colp2, colp3 = st.columns(3)
                with colp1:
                    perm_crear = st.checkbox("Crear", value=bool(permisos_actuales.get("crear", False)))
                    perm_editar = st.checkbox("Editar", value=bool(permisos_actuales.get("editar", False)))
                with colp2:
                    perm_eliminar = st.checkbox("Eliminar", value=bool(permisos_actuales.get("eliminar", False)))
                    perm_importar = st.checkbox("Importar", value=bool(permisos_actuales.get("importar", False)))
                with colp3:
                    perm_exportar = st.checkbox("Exportar", value=bool(permisos_actuales.get("exportar", True)))

                permisos_nuevos = {
                    "crear": perm_crear,
                    "editar": perm_editar,
                    "eliminar": perm_eliminar,
                    "importar": perm_importar,
                    "exportar": perm_exportar,
                }

                if st.session_state.rol == "superadministrador":
                    nuevo_rol = st.selectbox(
                        "Rol",
                        ["usuario", "administrador", "superadministrador"],
                        index=["usuario", "administrador", "superadministrador"].index(rol_actual_obj)
                        if rol_actual_obj in ["usuario", "administrador", "superadministrador"]
                        else 0,
                    )
                else:
                    nuevo_rol = None
                    st.info(f"Rol actual: {rol_actual_obj}")

                guardar_cambios = st.form_submit_button("ðŸ’¾ Guardar cambios del usuario")
                if guardar_cambios:
                    exito, msg = gestor_usuarios.actualizar_usuario(
                        usuario_obj,
                        contraseÃ±a=nueva_password if nueva_password else None,
                        rol=nuevo_rol,
                        email=nuevo_email,
                        puede_editar=puede_editar_obj,
                        permisos=permisos_nuevos,
                    )
                    if exito:
                        registrar_movimiento(
                            "Editar Usuario",
                            f"{usuario_obj} - rol={nuevo_rol or rol_actual_obj} - editar={puede_editar_obj} - permisos={permisos_nuevos}",
                        )
                        st.success(msg)
                        if usuario_obj == st.session_state.usuario_actual:
                            st.session_state.puede_editar = bool(puede_editar_obj)
                            if nuevo_rol:
                                st.session_state.rol = nuevo_rol
                        st.rerun()
                    else:
                        st.error(msg)

            if usuario_obj == st.session_state.usuario_actual:
                st.caption("No puedes eliminar tu propio usuario mientras estÃ¡s en sesiÃ³n.")
            elif usuario_obj in ["superadmin", "admin"]:
                st.caption("Las cuentas administrativas base estÃ¡n protegidas y no se pueden eliminar.")
            elif st.button("ðŸ—‘ï¸ Eliminar usuario seleccionado"):
                exito, msg = gestor_usuarios.eliminar_usuario(usuario_obj)
                if exito:
                    registrar_movimiento("Eliminar Usuario", usuario_obj)
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)
    else:
        st.info("Solo administradores o superadministradores pueden gestionar usuarios")

    st.markdown("---")
    st.subheader("ï¿½ Historial de Movimientos")
    if st.session_state.movimientos:
        df_mov = pd.DataFrame(st.session_state.movimientos)
        col_mov1, col_mov2 = st.columns([2, 1])
        with col_mov1:
            fecha_desde = st.date_input("Desde", value=datetime.now().date(), key="mov_desde")
        with col_mov2:
            fecha_hasta = st.date_input("Hasta", value=datetime.now().date(), key="mov_hasta")

        df_mov['_fecha_dt'] = pd.to_datetime(df_mov['fecha'], errors='coerce')
        inicio_dt = pd.to_datetime(fecha_desde)
        fin_dt = pd.to_datetime(fecha_hasta) + pd.Timedelta(days=1)
        df_mov_filtrado = df_mov[(df_mov['_fecha_dt'] >= inicio_dt) & (df_mov['_fecha_dt'] < fin_dt)].copy()
        df_mov_filtrado = df_mov_filtrado.drop(columns=['_fecha_dt']).sort_values(by='fecha', ascending=False).reset_index(drop=True)

        st.dataframe(df_mov_filtrado, width="stretch")

        if FPDF is not None and not df_mov_filtrado.empty:
            pdf_audit = FPDF(format='letter')
            pdf_audit.set_auto_page_break(auto=True, margin=15)
            pdf_audit.add_page()
            pdf_audit.set_font("Arial", "B", 12)
            pdf_audit.cell(0, 10, "Bitacora de Auditoria", ln=True, align='C')
            pdf_audit.set_font("Arial", size=10)
            pdf_audit.cell(0, 8, f"Rango: {fecha_desde} a {fecha_hasta}", ln=True)
            pdf_audit.ln(2)

            for _, row in df_mov_filtrado.iterrows():
                linea = f"{row.get('fecha')} | {row.get('usuario')} | {row.get('tipo')} | {row.get('detalle')}"
                pdf_audit.multi_cell(0, 7, linea)

            pdf_audit_bytes = pdf_audit.output(dest='S').encode('latin-1', errors='replace')
            st.download_button(
                label="ðŸ“„ Descargar Auditoria PDF",
                data=pdf_audit_bytes,
                file_name=f"auditoria_{fecha_desde}_{fecha_hasta}.pdf",
                mime="application/pdf",
                disabled=not permiso_exportar,
            )
    else:
        st.info("No hay movimientos registrados aÃºn")

    st.markdown("---")
    st.subheader("ï¿½ðŸ“Š Vista JSON de Datos")
    if st.session_state.planes:
        st.json(st.session_state.planes)
    else:
        st.info("No hay datos registrados")

# Footer
st.markdown("---")
st.markdown("""
<div style='text-align: center; color: gray; font-size: 12px;'>
    <p>Sistema de GestiÃ³n de Planes TelefÃ³nicos Corporativos con Gemini AI</p>
    <p>Ãšltima actualizaciÃ³n: 2024</p>
</div>
""", unsafe_allow_html=True)
