@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

echo ============================================
echo   竞品监控服务
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 首次运行，正在创建虚拟环境...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo 创建虚拟环境失败。请确认已安装 Python 3.10+ 并加入 PATH。
        pause
        exit /b 1
    )
    echo [2/3] 安装依赖（首次较慢，请耐心等）...
    .venv\Scripts\python.exe -m pip install -q --upgrade pip
    .venv\Scripts\python.exe -m pip install -q -r requirements.txt
    if errorlevel 1 (
        echo.
        echo 依赖安装失败。可以试试国内镜像：
        echo   .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        pause
        exit /b 1
    )
) else (
    echo [1/3] 虚拟环境已存在，跳过
)

if not exist ".env" (
    echo [2/3] 未找到 .env，正在从模板复制...
    copy /y ".env.example" ".env" >nul
    echo       已生成 .env。要收推送和用对话请先填好里面的配置。
) else (
    echo [2/3] .env 已存在
)

echo [3/3] 启动服务...
echo.
echo   看板：http://127.0.0.1:8100
echo   文档：http://127.0.0.1:8100/docs
echo   停止：按 Ctrl+C
echo.
echo ============================================
echo.

.venv\Scripts\python.exe run.py

echo.
echo 服务已退出。
pause
