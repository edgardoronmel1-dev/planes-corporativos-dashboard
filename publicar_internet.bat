@echo off
setlocal

echo ================================================
echo  Publicador de App - Streamlit + Cloudflare
echo ================================================
echo.

cd /d "%~dp0"

set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"

if not exist "cloudflared.exe" (
    echo ERROR: No se encontro cloudflared.exe en esta carpeta.
    echo Descargalo y vuelve a intentar.
    pause
    exit /b 1
)

if not exist "%PYTHON_EXE%" (
    echo ERROR: No se encontro .venv\Scripts\python.exe
    echo Ejecuta primero: instalar.bat
    pause
    exit /b 1
)

if not exist "app_planes_corporativos.py" (
    echo ERROR: No se encontro app_planes_corporativos.py en esta carpeta.
    pause
    exit /b 1
)

echo Verificando dependencias criticas...
"%PYTHON_EXE%" -c "import streamlit, pandas, plotly.express, requests, fpdf" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Faltan dependencias criticas para iniciar la app.
    echo Ejecuta primero: instalar.bat
    pause
    exit /b 1
)
echo OK - Dependencias verificadas
echo.

echo Iniciando Streamlit en puerto 8501...
start "Streamlit App" "%PYTHON_EXE%" -m streamlit run app_planes_corporativos.py --server.address 0.0.0.0 --server.port 8501

set "STREAMLIT_OK=0"
for /l %%i in (1,1,12) do (
    netstat -ano | findstr /r /c:":8501 .*LISTENING" >nul
    if not errorlevel 1 (
        set "STREAMLIT_OK=1"
        goto :streamlit_ready
    )
    timeout /t 1 /nobreak >nul
)

:streamlit_ready
if "%STREAMLIT_OK%"=="0" (
    echo ERROR: Streamlit no inicio en el puerto 8501.
    echo Revisa la ventana "Streamlit App" para ver el detalle del error.
    pause
    exit /b 1
)

echo OK - App iniciada correctamente en http://localhost:8501
echo.

echo Iniciando tunel publico con Cloudflare...
start "Cloudflare Tunnel" cmd /k "cd /d "%~dp0" && cloudflared.exe tunnel --url http://localhost:8501"

echo.
echo Listo. Revisa la ventana "Cloudflare Tunnel" para copiar la URL publica.
echo Manten ambas ventanas abiertas para que siga funcionando.
echo.
pause
