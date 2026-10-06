@echo off
rem ============================================================
rem  INSTALAR — Painel SISDPU numa maquina nova (Windows)
rem  Cria o .venv, instala dependencias, baixa o Chromium do
rem  Playwright e prepara o .env. Rode UMA vez apos descompactar.
rem ============================================================
setlocal
set ROOT=%~dp0
cd /d "%ROOT%"

echo ============================================================
echo   Instalando o Painel SISDPU
echo ============================================================
echo.

rem 1) Python no PATH
where python >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Python nao encontrado no PATH.
    echo         Instale Python 3.11+ ^(https://www.python.org^) e marque
    echo         "Add python.exe to PATH" durante a instalacao. Depois rode este .bat de novo.
    echo.
    pause
    exit /b 1
)

rem 2) Ambiente virtual
if exist ".venv\Scripts\python.exe" (
    echo [1/4] .venv ja existe — mantido.
) else (
    echo [1/4] Criando ambiente virtual .venv ...
    python -m venv .venv
    if errorlevel 1 ( echo [ERRO] Falha ao criar o .venv & pause & exit /b 1 )
)
set PY=.venv\Scripts\python.exe

rem 3) Dependencias
echo [2/4] Instalando dependencias ^(requirements.txt^) ...
"%PY%" -m pip install --upgrade pip >nul
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 ( echo [ERRO] Falha no pip install & pause & exit /b 1 )

rem 4) Chromium do Playwright
echo [3/4] Baixando o Chromium do Playwright ^(pode demorar^) ...
"%PY%" -m playwright install chromium
if errorlevel 1 ( echo [ERRO] Falha ao instalar o Chromium do Playwright & pause & exit /b 1 )

rem 5) .env a partir do modelo
echo [5/6] Preparando o .env ...
if exist ".env" (
    echo        .env ja existe — mantido ^(nao sobrescrevo^).
) else (
    copy /Y ".env.example" ".env" >nul
    echo        .env criado a partir do .env.example.
)

rem 6) Componente de leitura direta do e-proc (opcional — alimenta "Consultar
rem    processo", Triagem Inteligente de Intimacoes e Consulta de RPV/Precatorio).
rem    Caminho FIXO ja esperado pelo codigo (ingestao/eproc_mcp_client.py):
set EPROC_DEST=C:\Claude\MCP\eproc-trf4
echo [6/6] Componente de leitura do e-proc ...
if exist "%EPROC_DEST%" (
    echo        Ja existe em %EPROC_DEST% — mantido ^(nao sobrescrevo^).
) else if exist "%ROOT%..\eproc-trf4" (
    echo        Copiando para %EPROC_DEST% ...
    xcopy /E /I /Q "%ROOT%..\eproc-trf4" "%EPROC_DEST%" >nul
    if errorlevel 1 (
        echo        [AVISO] Falha ao copiar — recursos de e-proc ficarao indisponiveis.
    ) else (
        echo        Copiado. Falta so cadastrar suas credenciais ^(passo manual, abaixo^).
    )
) else (
    echo        Pasta "eproc-trf4" nao encontrada ao lado de "Projeto\" — pulado.
    echo        ^(Recursos de leitura direta do e-proc ficarao indisponiveis ate instalar.^)
)

echo.
echo ============================================================
echo   Instalacao concluida.
echo.
echo   AINDA FALTA ^(manual^):
echo    - Editar o .env: OFICIO_GERAL ^(pasta de dados PAJs/^) e
echo      credenciais/uso conforme necessario.
echo    - Instalar o Tesseract OCR ^(idioma "por"^) e deixar no PATH
echo      ^(https://github.com/UB-Mannheim/tesseract/wiki^).
echo    - Ter o Claude Code CLI instalado ^(para IA e elaboracao^).
echo    - Se o componente do e-proc foi copiado acima: cadastre suas
echo      credenciais rodando, uma vez, o comando abaixo (pede CPF/
echo      matricula, senha e a seed TOTP do e-proc):
echo        .venv\Scripts\python.exe C:\Claude\MCP\eproc-trf4\scripts\setup_totp.py
echo.
echo   Para abrir o painel: de duplo clique em Painel-SISDPU.bat
echo ============================================================
echo.
pause
endlocal
