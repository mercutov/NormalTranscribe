# Нормальный транскрайб (NormalTranscribe)

Портативная расшифровка роликов: GigaAM + Whisper + спикеры + сверка через Ollama.

## Установка для тестировщика (1 команда)

В **PowerShell**:

```powershell
irm https://raw.githubusercontent.com/mercutov/NormalTranscribe/master/install.ps1 | iex
```

По умолчанию ставит в `F:\NormalTranscribe` (если есть диск F:), иначе в `C:\NormalTranscribe`.

Другой путь:

```powershell
$env:NT_HOME = "D:\NormalTranscribe"
irm https://raw.githubusercontent.com/mercutov/NormalTranscribe/master/install.ps1 | iex
```

Скрипт скачает приложение и runtime с моделями из [Releases](https://github.com/mercutov/NormalTranscribe/releases).

## Обновления

При запуске exe сам проверяет GitHub Releases и предлагает скачать новую версию (runtime и папка «Заметки» не трогаются).

## Требования

- Windows + NVIDIA GPU ≥ 8 ГБ
- Путь **без кириллицы**
- По желанию: [Ollama](https://ollama.com) + `ollama pull qwen2.5:7b-instruct`

## Релиз для разработчика

```powershell
# полный (app + data parts) — для новых тестеров
.\runtime\python\python.exe pipeline\pack_release.py --app-root . --out-dir dist --version 1.2.0
gh release create v1.2.0 .\dist\NormalTranscribe-app-1.2.0.zip .\dist\NormalTranscribe-data-1.2.0.part*.zip --title "v1.2.0" --notes "..."

# только app — обычные обновления
.\runtime\python\python.exe pipeline\pack_release.py --app-root . --out-dir dist --version 1.2.1 --app-only
```
