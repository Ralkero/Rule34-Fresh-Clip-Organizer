@echo off
setlocal

echo Building Rule34 Fresh Clip Organizer GUI (portable folder)...

python -m pip install --upgrade pip
python -m pip install pyinstaller

pyinstaller ^
  --noconfirm ^
  --clean ^
  --onedir ^
  --windowed ^
  --name "Rule34Organizer" ^
  --add-data "r34_organizer.py;." ^
  --add-data "r34_config.json;." ^
  r34_gui.py

echo.
echo Copying important files next to the executable for easy editing...
copy /Y r34_organizer.py dist\Rule34Organizer\ >nul 2>&1
copy /Y r34_config.json dist\Rule34Organizer\ >nul 2>&1

echo.
echo Build finished.
echo Portable folder: dist\Rule34Organizer\
echo.
echo Note: r34_organizer.py and r34_config.json are now next to Rule34Organizer.exe
echo       so you can easily edit them.
echo.
pause
