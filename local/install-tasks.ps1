#Requires -Version 5.1
<#
.SYNOPSIS
    注册 4 个计划任务，替代 Linux 侧的 systemd timer。

.DESCRIPTION
    与 local/systemd/*.timer 一一对应，触发时刻保持同源：

        任务                    对应 timer                      时刻
        WorkBuddyAutoSignin     workbuddy-signin.timer          09:00            +0~1h 抖动
        WorkBuddyGrowthPoll     workbuddy-growth-poll.timer     01/05/09/13/17/21 +2min
        QoderDailyCredits       qoder-claim.timer               10:05/12:30/14:30/16:30 +0~1h
        CodeArtsDailyClaim      codearts-claim.timer            09:00/11:30/14:00/16:30 +0~1h

    两条设计决定，不要改回：

      1. 每个时刻用独立的 Daily 触发器，不要用「Daily + 每 N 小时重复一次」。
         后者一旦错过一次（关机/睡眠），当天后面的触发点会一起跳过；
         独立触发器之间互不影响，配合幂等接口就是天然的补领。

      2. 每个任务额外挂一个 AtLogOn 触发器（延迟 2 分钟）。
         这是 Linux `loginctl enable-linger` 的替代：Windows 上 DPAPI 解密钥
         要求用户已登录，登录后立刻补跑一次最稳妥。

.PARAMETER Python
    解释器路径（建议 pythonw.exe，避免弹黑框）。缺省依次尝试仓库内 .venv、
    venv、PATH 里的 pythonw.exe。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\local\install-tasks.ps1
#>
[CmdletBinding()]
param(
    [string]$Python
)

$ErrorActionPreference = "Stop"

$LocalDir = $PSScriptRoot
$RepoDir = Split-Path -Parent $LocalDir
$LogDir = Join-Path $LocalDir "logs"

function Write-Step {
    param([string]$Message)
    Write-Host "  $Message"
}

function Resolve-Pythonw {
    param([string]$Override)

    if ($Override) {
        if (-not (Test-Path -LiteralPath $Override)) {
            throw "解释器不存在：$Override"
        }
        return (Resolve-Path -LiteralPath $Override).Path
    }

    $candidates = @(
        (Join-Path $RepoDir ".venv\Scripts\pythonw.exe"),
        (Join-Path $RepoDir "venv\Scripts\pythonw.exe"),
        (Join-Path $LocalDir ".venv\Scripts\pythonw.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    throw "未找到 pythonw.exe。请用 -Python 指定，例如：-Python C:\Python312\pythonw.exe"
}

function New-DailyTriggerSet {
    param(
        [string[]]$Times,
        [string]$RandomDelay
    )

    $triggers = @()
    foreach ($time in $Times) {
        $trigger = New-ScheduledTaskTrigger -Daily -At $time
        if ($RandomDelay) {
            # MSFT_TaskDailyTrigger.RandomDelay 是 ISO 8601 duration 字符串
            $trigger.RandomDelay = $RandomDelay
        }
        $triggers += $trigger
    }
    return $triggers
}

# 任务定义：脚本 + 子命令 + 时刻 + 抖动
$definitions = @(
    @{
        Name        = "WorkBuddyAutoSignin"
        Script      = "workbuddy_run.py"
        Arguments   = "silent"
        Times       = @("09:00")
        RandomDelay = "PT1H"
        Description = "WorkBuddy 每日签到 + 成长中心全量（09:00 起 1 小时内随机）"
    },
    @{
        Name        = "WorkBuddyGrowthPoll"
        Script      = "workbuddy_run.py"
        Arguments   = "silent-poll"
        Times       = @("01:00", "05:00", "09:00", "13:00", "17:00", "21:00")
        RandomDelay = "PT2M"
        Description = "WorkBuddy 成长中心轮询（每 4 小时，含补签）"
    },
    @{
        Name        = "QoderDailyCredits"
        Script      = "qoder_run.py"
        Arguments   = "silent"
        Times       = @("10:05", "12:30", "14:30", "16:30")
        RandomDelay = "PT1H"
        Description = "Qoder 每日 100 Credits 领取（10:05 起 1 小时内随机，白天兜底）"
    },
    @{
        Name        = "CodeArtsDailyClaim"
        Script      = "codearts_run.py"
        Arguments   = "auto"
        Times       = @("09:00", "11:30", "14:00", "16:30")
        RandomDelay = "PT1H"
        Description = "CodeArts Agent 每日签到（09:00 起 1 小时内随机，白天兜底）"
    }
)

# ---------------------------------------------------------------- 前置检查

Write-Host "== 检查环境 =="

if (-not (Test-Path -LiteralPath (Join-Path $RepoDir "signin.py"))) {
    throw "未找到 signin.py，请确认本脚本位于仓库的 local\ 目录下"
}

$Pythonw = Resolve-Pythonw -Override $Python
Write-Step "解释器：$Pythonw"

foreach ($definition in $definitions) {
    $scriptPath = Join-Path $LocalDir $definition.Script
    if (-not (Test-Path -LiteralPath $scriptPath)) {
        throw "缺少入口脚本：$scriptPath"
    }
    Write-Step "入口存在：$($definition.Script)"
}

if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
Write-Step "运行身份：$CurrentUser（交互式，需已登录）"

# 凭据存在性只做提示，不阻断注册——用户可能稍后才登录客户端
$authHints = @(
    @{ Path = (Join-Path $env:LOCALAPPDATA "workbuddy-desktop.info"); Name = "WorkBuddy 凭据" },
    @{ Path = (Join-Path $env:APPDATA "com.qoder.app.stable");         Name = "Qoder 登录态目录" },
    @{ Path = (Join-Path $env:APPDATA "codearts-agent\Local State");  Name = "CodeArts 容器密钥" }
)
foreach ($hint in $authHints) {
    if (Test-Path -LiteralPath $hint.Path) {
        Write-Step "$($hint.Name)：已就绪"
    } else {
        Write-Step "$($hint.Name)：未找到（对应平台首次运行会报 NO_AUTH）"
    }
}

# ---------------------------------------------------------------- 注册

Write-Host ""
Write-Host "== 注册计划任务 =="

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$principal = New-ScheduledTaskPrincipal -UserId $CurrentUser `
    -LogonType Interactive -RunLevel Limited

# AtLogOn 触发器：登录后 2 分钟补跑（替代 Linux 的 linger）
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$logonTrigger.Delay = "PT2M"

foreach ($definition in $definitions) {
    $scriptPath = Join-Path $LocalDir $definition.Script
    $argument = '"{0}" {1}' -f $scriptPath, $definition.Arguments

    $action = New-ScheduledTaskAction -Execute $Pythonw `
        -Argument $argument -WorkingDirectory $RepoDir

    $triggers = @(New-DailyTriggerSet -Times $definition.Times `
        -RandomDelay $definition.RandomDelay)
    $triggers += $logonTrigger

    Register-ScheduledTask -TaskName $definition.Name `
        -Action $action -Trigger $triggers -Settings $settings `
        -Principal $principal -Description $definition.Description `
        -Force | Out-Null

    # 回读校验：注册接口返回成功不等于配置生效
    $task = Get-ScheduledTask -TaskName $definition.Name
    $info = Get-ScheduledTaskInfo -TaskName $definition.Name

    $logonCount = @($task.Triggers |
        Where-Object { $_.CimClass.CimClassName -eq "MSFT_TaskLogonTrigger" }).Count
    if ($logonCount -lt 1) {
        throw "$($definition.Name)：登录触发器未写入"
    }

    $dailyCount = @($task.Triggers |
        Where-Object { $_.CimClass.CimClassName -eq "MSFT_TaskDailyTrigger" }).Count
    if ($dailyCount -ne $definition.Times.Count) {
        throw ("$($definition.Name)：Daily 触发器数量为 $dailyCount，应为 " +
               "$($definition.Times.Count)")
    }

    $next = $null
    try {
        $next = [datetime]$info.NextRunTime
    } catch {
        $next = $null
    }
    if (-not $next -or $next.Year -lt 2000) {
        throw "$($definition.Name)：NextRunTime 无效（$($info.NextRunTime)），任务不会按时触发"
    }

    Write-Step ("{0}：{1} 个 Daily + 1 个 LogOn，下次 {2:yyyy-MM-dd HH:mm}" -f `
        $definition.Name, $dailyCount, $next)
}

# ---------------------------------------------------------------- 验收提示

Write-Host ""
Write-Host "== 已注册 =="
Get-ScheduledTask -TaskName ($definitions | ForEach-Object { $_.Name }) |
    Select-Object TaskName, State |
    Format-Table -AutoSize

Write-Host "查看下次触发时刻："
Write-Host "  Get-ScheduledTaskInfo -TaskName WorkBuddyAutoSignin | Select NextRunTime"
Write-Host "全量体检："
Write-Host "  powershell -ExecutionPolicy Bypass -File .\local\status.ps1"
