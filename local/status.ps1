#Requires -Version 5.1
<#
.SYNOPSIS
    四任务 + 三平台体检。

.DESCRIPTION
    两块内容：

      1. 计划任务状态：State / LastRunTime / LastTaskResult / NextRunTime。
      2. 三平台登录态：依次调用各入口的 doctor，解析那一行 JSON。

    必须识别「从未运行」的占位值，否则会把没跑过的任务误报成成功：

        LastTaskResult = 267011  (0x00041303, SCHED_S_TASK_HAS_NOT_RUN)
        LastRunTime    = 1999-11-30

    只看 LastTaskResult == 0 也会误判——任务可能压根没被触发过。

.PARAMETER Python
    解释器路径。缺省依次尝试仓库内 .venv、venv、PATH 里的 python.exe。
    体检需要读 stdout，所以用 python.exe 而不是 pythonw.exe。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\local\status.ps1
#>
[CmdletBinding()]
param(
    [string]$Python
)

$ErrorActionPreference = "Stop"

# 子进程输出是 UTF-8，PS 5.1 默认按 OEM 代码页解码，中文会变乱码
$env:PYTHONIOENCODING = "utf-8"
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # 无控制台（如被计划任务调用）时允许失败
}

$LocalDir = $PSScriptRoot
$RepoDir = Split-Path -Parent $LocalDir

$TaskNames = @(
    "WorkBuddyAutoSignin",
    "WorkBuddyGrowthPoll",
    "QoderDailyCredits",
    "CodeArtsDailyClaim"
)

$Checks = @(
    @{ Name = "WorkBuddy"; Script = "workbuddy_run.py"; Arg = "doctor" },
    @{ Name = "Qoder";     Script = "qoder_run.py";     Arg = "doctor" },
    @{ Name = "CodeArts";  Script = "codearts_run.py";  Arg = "doctor" }
)

function Resolve-Python {
    param([string]$Override)

    if ($Override) {
        if (-not (Test-Path -LiteralPath $Override)) {
            throw "解释器不存在：$Override"
        }
        return (Resolve-Path -LiteralPath $Override).Path
    }

    $candidates = @(
        (Join-Path $RepoDir ".venv\Scripts\python.exe"),
        (Join-Path $RepoDir "venv\Scripts\python.exe"),
        (Join-Path $LocalDir ".venv\Scripts\python.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    throw "未找到 python.exe，请用 -Python 指定"
}

function Format-TaskResult {
    param($Code, $LastRunTime)

    $last = $null
    if ($LastRunTime) {
        try {
            $last = [datetime]$LastRunTime
        } catch {
            $last = $null
        }
    }

    # 从未运行的两种表示，必须先判断，不能落到下面的 0 == 成功分支
    if ($Code -eq 267011) {
        return "从未运行"
    }
    if (-not $last -or $last.Year -lt 2000) {
        return "从未运行"
    }

    switch ($Code) {
        0       { return "成功" }
        267009  { return "正在运行" }
        267010  { return "被用户停止" }
        267014  { return "被终止" }
        default {
            if ($null -eq $Code) {
                return "未知"
            }
            return ("失败 (0x{0:X8})" -f [uint32]$Code)
        }
    }
}

function Format-Time {
    param($Value)
    if (-not $Value) {
        return "-"
    }
    try {
        $dt = [datetime]$Value
    } catch {
        return "-"
    }
    if ($dt.Year -lt 2000) {
        return "-"
    }
    return $dt.ToString("MM-dd HH:mm")
}

# ---------------------------------------------------------------- 计划任务

Write-Host "== 计划任务 =="
Write-Host ("{0,-22} {1,-10} {2,-14} {3,-18} {4}" -f `
    "任务", "状态", "上次运行", "上次结果", "下次运行")

$problems = 0

foreach ($name in $TaskNames) {
    $task = $null
    try {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
    } catch {
        Write-Host ("{0,-22} {1}" -f $name, "未注册")
        $problems++
        continue
    }

    $info = Get-ScheduledTaskInfo -TaskName $name
    $result = Format-TaskResult -Code $info.LastTaskResult `
        -LastRunTime $info.LastRunTime

    # State 为 Disabled 时，NextRunTime 可能仍是历史值，必须单独看
    if ($task.State -eq "Disabled") {
        $result = "已禁用"
        $problems++
    } elseif ($result -ne "成功" -and $result -ne "正在运行") {
        $problems++
    }

    Write-Host ("{0,-22} {1,-10} {2,-14} {3,-18} {4}" -f `
        $name, $task.State, (Format-Time $info.LastRunTime), $result,
        (Format-Time $info.NextRunTime))
}

# ---------------------------------------------------------------- 三平台

Write-Host ""
Write-Host "== 登录态与接口 =="
Write-Host ("{0,-12} {1,-14} {2}" -f "平台", "结果", "说明")

$python = $null
try {
    $python = Resolve-Python -Override $Python
} catch {
    Write-Host $_.Exception.Message
    $problems++
}

if ($python) {
    foreach ($check in $Checks) {
        $scriptPath = Join-Path $LocalDir $check.Script
        if (-not (Test-Path -LiteralPath $scriptPath)) {
            Write-Host ("{0,-12} {1,-14} {2}" -f $check.Name, "MISSING", "缺少 $($check.Script)")
            $problems++
            continue
        }

        $output = @()
        $exitCode = 0
        try {
            # doctor 的诊断信息走 stderr，这里只取 stdout 的 JSON 行
            $output = & $python $scriptPath $check.Arg 2>$null
            $exitCode = $LASTEXITCODE
        } catch {
            Write-Host ("{0,-12} {1,-14} {2}" -f `
                $check.Name, "ERROR", "调用失败：$($_.Exception.Message)")
            $problems++
            continue
        }

        $line = @($output | Where-Object { $_ -and $_.Trim() }) | Select-Object -Last 1
        $result = "UNKNOWN"
        $report = "未输出可解析的 JSON（退出码 $exitCode）"
        if ($line) {
            try {
                $obj = $line | ConvertFrom-Json
                if ($obj.result) {
                    $result = [string]$obj.result
                }
                if ($obj.report) {
                    $report = [string]$obj.report
                } else {
                    $report = ""
                }
            } catch {
                $report = "输出不是 JSON：$($line.Substring(0, [Math]::Min(80, $line.Length)))"
            }
        }

        if ($result -ne "AUTH_READY" -and $result -ne "OK") {
            $problems++
        }
        Write-Host ("{0,-12} {1,-14} {2}" -f $check.Name, $result, $report)
    }
}

# ---------------------------------------------------------------- 日志尾部

$logFiles = @("workbuddy_signin.log", "qoder_signin.log", "codearts_signin.log")
foreach ($file in $logFiles) {
    $path = Join-Path $LocalDir "logs\$file"
    if (-not (Test-Path -LiteralPath $path)) {
        continue
    }
    $tail = Get-Content -LiteralPath $path -Tail 1 -Encoding UTF8
    if ($tail) {
        Write-Host ""
        Write-Host "$file 末行：$tail"
    }
}

Write-Host ""
if ($problems -gt 0) {
    Write-Host "存在 $problems 项需要处理。"
    exit 1
}
Write-Host "全部正常。"
exit 0
