# 构建说明

## 1. Windows 独立版 (.exe)

要构建用户无需安装 Python 即可运行的独立 Windows 可执行文件：

1. **安装 PyInstaller**：
    ```bash
    pip install pyinstaller
    ```

2. **构建可执行文件**：
    在项目根目录运行以下命令：
    ```bash
    pyinstaller build_windows.spec
    ```

3. **找到输出文件**：
    可执行文件将位于 LimitUpSniper.exe。
    你可以将整个 LimitUpSniper 文件夹压缩后分发。

4. **使用方法**：
    - 运行 `LimitUpSniper.exe`。
    - 它会自动启动服务器并打开默认的网页浏览器。
    - 进入 **设置**（齿轮图标）-> **API 设置**，输入你的 DeepSeek API 密钥。

## 2. Android (PWA)

由于应用程序需要 Python 后端（FastAPI、Pandas 等），在 Android 上原生运行需要一个 Python 环境。

### 选项 A：渐进式 Web 应用（推荐）
这是最简单的方法。你可以在 PC 或云端托管服务器，然后通过手机访问。

1. **托管服务器**：
    在 PC 或云服务器上运行服务器（例如，运行 `python run_desktop.py` 或 `uvicorn app.main:app --host 0.0.0.0`）。
    * 确保你的 PC 和手机在同一个 Wi-Fi 网络下。
    * 找到你的 PC 的 IP 地址（例如，在 Windows 上运行 `ipconfig`）。

2. **在 Android 上访问**：
    - 在 Android 设备上打开 Chrome 浏览器。
    - 访问 `http://<你的_PC_IP>:8000`（例如，`http://192.168.1.5:8000`）。
    - 点击浏览器菜单（三个点）-> **“添加到主屏幕”**（或“安装应用”）。

3. **体验**：
    - 主屏幕上会出现一个图标。
    - 打开后，它看起来和原生应用一样（全屏，无地址栏）。
    - 你可以在设置菜单中输入 API 密钥，与桌面版操作相同。

### 选项 B：在 Android 上本地运行（高级）
如果你确实希望在 Android 上进行“本地计算”（无需 PC），可以使用 **Termux**。

1. **从 F-Droid 或 Google Play 安装 Termux**。
2. **安装 Python 和 Git**：
    ```bash
    pkg install python git
    ```
3. **克隆并安装**：
    ```bash
    git clone https://github.com/ForceMind/Limit-Up-Sniper.git
    cd Limit-Up-Sniper
    pip install -r requirements.txt
    ```
4. **运行**：
    ```bash
    python run_desktop.py
    ```
5. **访问**：
    打开 Chrome 浏览器，访问 `http://localhost:8000`。

## 关于 API 密钥的说明
- **服务器版本**：使用 `DEEPSEEK_API_KEY` 环境变量。
- **独立版/客户端版本**：用户可以在设置菜单中手动输入 API 密钥。该密钥存储在浏览器的本地存储中，并随分析请求一起发送。

# Build Instructions

## 1. Windows Standalone (.exe)

To build a standalone Windows executable that users can run without installing Python:

1.  **Install PyInstaller**:
    ```bash
    pip install pyinstaller
    ```

2.  **Build the Executable**:
    Run the following command in the project root:
    ```bash
    pyinstaller build_windows.spec
    ```

3.  **Locate the Output**:
    The executable will be in `dist/LimitUpSniper/LimitUpSniper.exe`.
    You can zip the entire `dist/LimitUpSniper` folder and distribute it.

4.  **Usage**:
    -   Run `LimitUpSniper.exe`.
    -   It will automatically start the server and open your default web browser.
    -   Go to **Settings** (gear icon) -> **API Settings** to enter your DeepSeek API Key.

## 2. Android (PWA)

Since the application requires a Python backend (FastAPI, Pandas, etc.), running it natively on Android requires a Python environment.

### Option A: Progressive Web App (Recommended)
This is the easiest way. You host the server on your PC or Cloud, and access it from your phone.

1.  **Host the Server**:
    Run the server on your PC or a cloud server (e.g., `python run_desktop.py` or `uvicorn app.main:app --host 0.0.0.0`).
    *   Ensure your PC and Phone are on the same Wi-Fi.
    *   Find your PC's IP address (e.g., `ipconfig` on Windows).

2.  **Access on Android**:
    -   Open Chrome on your Android device.
    -   Navigate to `http://<YOUR_PC_IP>:8000` (e.g., `http://192.168.1.5:8000`).
    -   Tap the browser menu (three dots) -> **"Add to Home Screen"** (or "Install App").

3.  **Experience**:
    -   An icon will appear on your home screen.
    -   When opened, it will look and feel like a native app (full screen, no address bar).
    -   You can input your API Key in the Settings menu, just like on desktop.

### Option B: Run Locally on Android (Advanced)
If you truly want "local computation" on Android (no PC required), you can use **Termux**.

1.  **Install Termux** from F-Droid or Google Play.
2.  **Install Python & Git**:
    ```bash
    pkg install python git
    ```
3.  **Clone & Install**:
    ```bash
    git clone https://github.com/ForceMind/Limit-Up-Sniper.git
    cd Limit-Up-Sniper
    pip install -r requirements.txt
    ```
4.  **Run**:
    ```bash
    python run_desktop.py
    ```
5.  **Access**:
    Open Chrome and go to `http://localhost:8000`.

## Note on API Keys
-   **Server Version**: Uses the `DEEPSEEK_API_KEY` environment variable.
-   **Standalone/Client Version**: Users can manually enter their API Key in the Settings menu. This key is stored in the browser's local storage and sent with analysis requests.
