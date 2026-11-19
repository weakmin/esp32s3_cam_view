# 编译 esp32s3_cam_view（IDF 装在 C:\esp）
$env:IDF_PATH = 'C:\esp\esp-idf'
$env:IDF_TOOLS_PATH = 'C:\esp\espressif'
$env:IDF_PYTHON_ENV_PATH = 'C:\esp\espressif\python_env\idf5.3_py3.10_env'
$env:HTTP_PROXY = 'http://127.0.0.1:7897'
$env:HTTPS_PROXY = 'http://127.0.0.1:7897'

. C:\esp\esp-idf\export.ps1
Set-Location $PSScriptRoot

if (-not (Test-Path build)) {
    idf.py set-target esp32s3
}
idf.py build
