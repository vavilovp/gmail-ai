# Добавляет задачу в Планировщик Windows:
# при входе в систему (через 1 минуту) спросить «Разобрать почту?» и, если да, запустить весь цикл.
param(
    [string]$User = "$env:USERDOMAIN\$env:USERNAME",
    [switch]$Remove
)
$TaskName = "Разбор почты (локальная LLM)"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    $a = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -User `"$User`""
    if ($Remove) { $a += " -Remove" }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $a
    exit
}

try {
    if ($Remove) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "Задача «$TaskName» удалена." -ForegroundColor Green
    }
    else {
        $script = Join-Path $PSScriptRoot "gmail_ai.py"
        if (-not (Test-Path $script)) { throw "Не найден $script" }
        if (-not (Test-Path (Join-Path $PSScriptRoot "config.json"))) {
            throw "Нет config.json рядом со скриптом. Сначала заполните его и проверьте mail-test.bat."
        }

        # pyw.exe — запуск Python без чёрного окна консоли
        $pyw = (Get-Command pyw.exe -ErrorAction SilentlyContinue).Source
        if (-not $pyw) {
            $py = (& py -c "import sys; print(sys.executable)") 2>$null
            if ($py) { $pyw = Join-Path (Split-Path $py) "pythonw.exe" }
        }
        if (-not $pyw -or -not (Test-Path $pyw)) { throw "Не найден pyw.exe / pythonw.exe. Установлен ли Python?" }

        $action    = New-ScheduledTaskAction -Execute $pyw -Argument "`"$script`" --ask" -WorkingDirectory $PSScriptRoot
        $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $User
        $trigger.Delay = "PT1M"
        $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
        $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                        -StartWhenAvailable -MultipleInstances IgnoreNew `
                        -ExecutionTimeLimit (New-TimeSpan -Hours 2)

        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null

        Write-Host "Готово: задача «$TaskName» добавлена." -ForegroundColor Green
        Write-Host "Пользователь: $User"
        Write-Host "Запуск: $pyw `"$script`" --ask"
        Write-Host "Срабатывает через 1 минуту после входа в Windows."
        Write-Host "Проверить сейчас: Планировщик заданий -> «$TaskName» -> Выполнить."
    }
}
catch {
    Write-Host "Ошибка: $_" -ForegroundColor Red
}
Read-Host "`nНажмите Enter, чтобы закрыть"
