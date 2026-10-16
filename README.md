# Нормальный транскрайб (NormalTranscribe)

Портативная расшифровка роликов: GigaAM + Whisper + спикеры + сверка через Ollama.

## Для тестеров

1. Один раз получи полную папку `D:\NormalTranscribe` (с `runtime`).
2. Обновления: при запуске приложение само предложит скачать новый `NormalTranscribe-app-*.zip` из [Releases](https://github.com/mercutov/NormalTranscribe/releases).  
   `runtime`, `Заметки` и `Ролики` не перезаписываются.

## Требования

- NVIDIA GPU ≥ 8 ГБ
- Путь **без кириллицы**
- Опционально: [Ollama](https://ollama.com) + модель (например `qwen2.5:7b-instruct`)

## Релиз для разработчика

```powershell
.\Собрать релиз.ps1 -Version 1.1.0
gh release create v1.1.0 .\dist\NormalTranscribe-app-1.1.0.zip --title "v1.1.0" --notes "..."
```
