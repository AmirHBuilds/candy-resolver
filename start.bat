@echo off
rem One-click start (Windows). Needs Docker Desktop.
cd /d "%~dp0"
where docker >nul 2>nul || (echo Docker is not installed. Install Docker Desktop first. & pause & exit /b 1)
if exist .env goto run
set /p KEY=Paste your TMDB API key and press Enter: 
> .env echo TMDB_API_KEY=%KEY%
:run
docker compose up -d --build --wait
if errorlevel 1 (echo Something failed - see the messages above. & pause & exit /b 1)
echo.
echo candyresolver is running: http://localhost:8000/docs
echo Your admin token:
docker compose exec -T api python -c "from app.config import settings, ensure_secrets; ensure_secrets(); print(settings.admin_token)"
pause
