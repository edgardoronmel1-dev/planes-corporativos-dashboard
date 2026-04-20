@echo off
setlocal EnableExtensions EnableDelayedExpansion

echo ========================================
echo Publicar cambios en GitHub
echo ========================================
echo.

cd /d "%~dp0"

set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    where python >nul 2>&1
    if errorlevel 1 (
        echo ERROR: No se encontro Python para validar sintaxis.
        echo Instala Python o ejecuta primero: instalar.bat
        pause
        exit /b 1
    )
    set "PYTHON_EXE=python"
)

where git >nul 2>&1
if errorlevel 1 (
    echo ERROR: Git no esta instalado o no esta en PATH.
    pause
    exit /b 1
)

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo ERROR: Esta carpeta no es un repositorio Git.
    pause
    exit /b 1
)

git remote get-url origin >nul 2>&1
if errorlevel 1 (
    echo ERROR: No existe remoto 'origin'.
    echo Configuralo primero con: git remote add origin URL_DEL_REPO
    pause
    exit /b 1
)

for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%b"
if "%BRANCH%"=="" (
    echo ERROR: No se pudo detectar la rama actual.
    pause
    exit /b 1
)

echo Rama actual: %BRANCH%
echo.
set "COMMIT_MSG=Actualizacion automatica %date% %time%"
echo Mensaje de commit: %COMMIT_MSG%

echo.
echo Agregando cambios...
git add -A
if errorlevel 1 (
    echo ERROR: Fallo git add.
    pause
    exit /b 1
)

git diff --cached --quiet
if not errorlevel 1 (
    echo No hay cambios para subir.
    pause
    exit /b 0
)

echo Validando sintaxis de archivos Python en cambios...
set "HAS_PY_FILES=0"
for /f "delims=" %%f in ('git diff --cached --name-only -- "*.py"') do (
    set "HAS_PY_FILES=1"
    if exist "%%f" (
        "%PYTHON_EXE%" -m py_compile "%%f" >nul 2>&1
        if errorlevel 1 (
            echo ERROR: Se detecto un error de sintaxis en %%f
            echo Detalle:
            "%PYTHON_EXE%" -m py_compile "%%f"
            pause
            exit /b 1
        )
    )
)
if "!HAS_PY_FILES!"=="1" (
    echo OK - Sintaxis Python valida
) else (
    echo No hay archivos Python para validar
)

echo Validando dependencias criticas de la app...
"%PYTHON_EXE%" -c "import streamlit, pandas, plotly.express, requests, fpdf" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Faltan dependencias criticas para ejecutar la app.
    echo Ejecuta: instalar.bat
    pause
    exit /b 1
)
echo OK - Dependencias criticas detectadas

echo Creando commit...
git commit -m "%COMMIT_MSG%"
if errorlevel 1 (
    echo ERROR: No se pudo crear el commit.
    pause
    exit /b 1
)

echo Subiendo a GitHub...
git push -u origin %BRANCH%
if errorlevel 1 (
    echo ERROR: Fallo el push a GitHub.
    pause
    exit /b 1
)

echo.
echo ========================================
echo Publicacion completada correctamente
echo ========================================
echo.
pause
exit /b 0
