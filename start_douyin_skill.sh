#!/bin/bash

# 抖音视频下载器 Skill 启动脚本 (Linux/Mac)

set -euo pipefail

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================="
echo "   抖音视频下载器 Skill 启动器"
echo "=========================================="
echo

# 检查虚拟环境
if [ ! -f ".venv/bin/activate" ]; then
    echo "[错误] 虚拟环境未找到！"
    echo "请先创建虚拟环境: python3 -m venv .venv"
    exit 1
fi

# 激活虚拟环境
source .venv/bin/activate

# 检查 Python
if ! command -v python &> /dev/null; then
    echo "[错误] Python 未找到！"
    exit 1
fi

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 日志函数
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查端口是否被占用
check_port() {
    local port=$1
    if command -v lsof &> /dev/null; then
        lsof -Pi :$port -sTCP:LISTEN -t &> /dev/null
        return $?
    elif command -v netstat &> /dev/null; then
        netstat -tuln 2>/dev/null | grep -q ":$port "
        return $?
    else
        # 如果无法检查，返回未占用
        return 1
    fi
}

# 强制关闭占用端口的进程
kill_port() {
    local port=$1
    log_info "检查端口 $port..."
    
    if check_port $port; then
        log_warn "端口 $port 被占用，正在关闭..."
        if command -v lsof &> /dev/null; then
            # 使用 lsof 查找并关闭进程
            local pids=$(lsof -t -i:$port 2>/dev/null)
            if [ -n "$pids" ]; then
                echo "$pids" | xargs kill -9 2>/dev/null || true
                sleep 2
                log_info "端口 $port 已释放"
            fi
        elif command -v fuser &> /dev/null; then
            # 使用 fuser 关闭进程
            fuser -k $port/tcp 2>/dev/null || true
            sleep 2
            log_info "端口 $port 已释放"
        fi
    else
        log_info "端口 $port 可用"
    fi
}

# 显示菜单
show_menu() {
    echo
    echo "请选择启动模式:"
    echo
    echo "1. 启动 Web API 服务 (端口 8080)"
    echo "2. 启动前端开发服务器 (端口 3000)"
    echo "3. 启动 Web API + 前端 (推荐)"
    echo "4. 检查依赖"
    echo "5. 退出"
    echo
}

# 检查依赖
check_dependencies() {
    echo
    log_info "检查依赖..."
    echo

    # 检查 Python 包
    echo "[1/5] 检查 Python 包..."
    if python -c "import fastapi, uvicorn, pydantic, aiosqlite, aiofiles" 2>/dev/null; then
        log_info "Python 包已安装"
    else
        log_warn "缺少 Python 包，正在安装..."
        pip install fastapi uvicorn pydantic aiosqlite aiofiles
    fi

    # 检查 F2
    echo
    echo "[2/5] 检查 F2 框架..."
    if python -c "import f2" 2>/dev/null; then
        log_info "F2 框架已安装"
    else
        log_error "F2 框架未安装！"
        echo "请运行: pip install f2"
        return 1
    fi

    # 检查 Node.js
    echo
    echo "[3/5] 检查 Node.js..."
    if command -v node &> /dev/null; then
        NODE_VERSION=$(node --version)
        log_info "Node.js 版本: $NODE_VERSION"
    else
        log_error "Node.js 未安装！"
        echo "请从 https://nodejs.org/ 安装 Node.js"
        return 1
    fi

    # 检查前端依赖
    echo
    echo "[4/5] 检查前端依赖..."
    if [ ! -f "frontend/package.json" ]; then
        log_error "前端目录未找到！"
        return 1
    fi

    if [ -d "frontend/node_modules" ]; then
        log_info "前端依赖已安装"
    else
        log_warn "前端依赖未安装"
        echo "请运行: cd frontend && npm install"
    fi

    # 检查 Playwright
    echo
    echo "[5/5] 检查 Playwright 浏览器..."
    if python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); p.chromium.launch(); p.stop()" 2>/dev/null; then
        log_info "Playwright 浏览器已安装"
    else
        log_warn "Playwright 浏览器未安装"
        echo "请运行: playwright install chromium"
        echo "或: python -m playwright install chromium"
    fi

    echo
    log_info "依赖检查完成！"
    echo
}

start_api() {
    echo
    log_info "启动 Web API 服务..."

    # 强制关闭占用端口的进程
    kill_port 8080

    echo "访问地址: http://localhost:8080"
    echo "API文档: http://localhost:8080/docs"
    echo
    echo "按 Ctrl+C 停止服务"
    echo

    python -m skills.douyin_downloader.web --port 8080
}

start_frontend() {
    echo
    log_info "启动前端开发服务器..."

    if [ ! -d "frontend" ]; then
        log_error "前端目录不存在！"
        return 1
    fi

    # 强制关闭占用端口的进程
    kill_port 3000

    cd frontend

    # 检查 node_modules
    if [ ! -d "node_modules" ]; then
        log_warn "前端依赖未安装，正在安装..."
        npm install
        if [ $? -ne 0 ]; then
            log_error "npm install 失败！"
            cd ..
            return 1
        fi
    fi

    echo "访问地址: http://localhost:3000"
    echo
    echo "[注意] 确保后端服务已在端口 8080 上运行"
    echo "按 Ctrl+C 停止服务"
    echo

    npm run dev
    cd ..
}

start_both() {
    echo
    log_info "启动 Web API + 前端服务..."
    echo

    # 检查依赖
    echo "检查依赖..."
    python -c "import fastapi, uvicorn, pydantic" 2>/dev/null || {
        log_warn "安装后端依赖..."
        pip install fastapi uvicorn pydantic aiosqlite aiofiles
    }

    python -c "import f2" 2>/dev/null || {
        log_error "F2 框架未安装！"
        echo "请运行: pip install f2"
        return 1
    }

    # 检查并安装前端依赖
    if [ -d "frontend" ] && [ ! -d "frontend/node_modules" ]; then
        log_warn "安装前端依赖..."
        cd frontend
        npm install
        if [ $? -ne 0 ]; then
            log_error "npm install 失败！"
            cd ..
            return 1
        fi
        cd ..
    fi

    # 强制关闭占用端口的进程
    kill_port 8080
    kill_port 3000

    # 创建临时目录
    TEMP_DIR=$(mktemp -d)
    trap "rm -rf $TEMP_DIR" EXIT

    # 启动后端
    log_info "启动后端服务..."
    (
        cd "$SCRIPT_DIR"
        source .venv/bin/activate
        echo "========================================"
        echo "后端服务 - 端口 8080"
        echo "========================================"
        python -m skills.douyin_downloader.web --port 8080
    ) > "$TEMP_DIR/backend.log" 2>&1 &
    API_PID=$!

    # 等待后端启动
    log_info "等待后端初始化..."
    for i in {1..15}; do
        sleep 1
        if curl -s http://localhost:8080/ > /dev/null 2>&1; then
            log_info "后端服务已就绪"
            break
        fi
        if [ $i -eq 15 ]; then
            log_warn "后端服务启动超时，日志如下:"
            cat "$TEMP_DIR/backend.log"
        fi
    done

    # 启动前端
    if [ -d "frontend" ]; then
        log_info "启动前端服务..."
        (
            cd "$SCRIPT_DIR/frontend"
            echo "========================================"
            echo "前端服务"
            echo "========================================"
            npm run dev
        ) > "$TEMP_DIR/frontend.log" 2>&1 &
        FRONTEND_PID=$!

        log_info "等待前端初始化..."
        sleep 3
    fi

    echo
    echo "=========================================="
    echo "服务已启动:"
    echo "- API: http://localhost:8080"
    echo "- Web: http://localhost:3000"
    echo "=========================================="
    echo
    echo "按 Ctrl+C 停止所有服务"
    echo

    # 处理信号
    cleanup() {
        echo
        log_info "正在停止服务..."
        kill $API_PID 2>/dev/null || true
        [ -n "${FRONTEND_PID:-}" ] && kill $FRONTEND_PID 2>/dev/null || true
        wait
        log_info "服务已停止"
        exit 0
    }

    trap cleanup INT TERM

    # 监控进程
    while true; do
        if ! kill -0 $API_PID 2>/dev/null; then
            log_error "后端服务已停止"
            [ -n "${FRONTEND_PID:-}" ] && kill $FRONTEND_PID 2>/dev/null || true
            break
        fi
        sleep 1
    done
}

# 主逻辑
if [ -z "${1:-}" ]; then
    show_menu
    read -p "请输入选项 (1-5): " choice
else
    choice=$1
fi

case $choice in
    1)
        start_api
        ;;
    2)
        start_frontend
        ;;
    3)
        start_both
        ;;
    4)
        check_dependencies
        read -p "按回车键继续..."
        show_menu
        read -p "请输入选项 (1-5): " choice
        case $choice in
            1) start_api ;;
            2) start_frontend ;;
            3) start_both ;;
            4) check_dependencies ;;
            5) exit 0 ;;
            *) echo "无效选项！"; exit 1 ;;
        esac
        ;;
    5)
        exit 0
        ;;
    *)
        echo "无效选项！"
        exit 1
        ;;
esac
