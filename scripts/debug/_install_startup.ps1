$WS = New-Object -ComObject WScript.Shell
$SC = $WS.CreateShortcut([Environment]::GetFolderPath('Startup') + '\QuantTrading.lnk')
$SC.TargetPath = 'C:\Users\zhu\quant_trading\start.bat'
$SC.WorkingDirectory = 'C:\Users\zhu\quant_trading'
$SC.WindowStyle = 7
$SC.Save()
Write-Output ("OK: " + $SC.FullName)
