import glob
import subprocess
import shutil
import os
import sys
import httpx
import platform
import base64
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
is_windows = platform.system() == "Windows"

# 资源和输出路径
APK_DIR = os.path.join(PROJECT_ROOT, "apk")
DIST_DIR = os.path.join(PROJECT_ROOT, "dist")
BUILD_TEMP_DIR = os.path.join(PROJECT_ROOT, "build_temp")
RESOURCES_DIR = os.path.join(PROJECT_ROOT, "resources")
LIB_DIR = os.path.join(RESOURCES_DIR, "lib")
KEYSTORE_FILE = os.path.join(RESOURCES_DIR, "keystore", "tsk_mod.keystore")
KEYSTORE_ALIAS = "my-key-alias"


# --- 全局变量 ---
APK_URL = "https://dl-app.games.dmm.com/android/jp.co.fanzagames.twinklestarknightsx_a"
VERSION_API_URL = "https://api.store.games.dmm.com/freeapp/705566"
app_version = ""
build_tools_version = "36.1.0"


def run_cmd(cmd):
    """运行一个子进程命令并检查结果"""
    print(f"[*] 执行: {' '.join(cmd)}")
    subprocess.run(cmd, check=True, shell=is_windows, input="\n", text=True)


def get_version():
    """使用 httpx 获取最新游戏版本号"""
    global app_version
    if app_version:
        return app_version

    print("[*] 正在获取最新游戏版本...", file=sys.stderr)
    try:
        with httpx.Client() as client:
            response = client.get(VERSION_API_URL, timeout=10.0)
            response.raise_for_status()
            data = response.json()

        app_version = data["free_appinfo"]["app_version_name"]
        print(f"[+] 最新版本: {app_version}", file=sys.stderr)
        return app_version
    except httpx.HTTPStatusError as e:
        print(
            f"[!] 获取版本失败 (HTTP错误): {e.response.status_code} - {e.request.url}",
            file=sys.stderr,
        )
        exit(1)
    except Exception as e:
        print(f"[!] 获取版本失败 (其他错误): {e}", file=sys.stderr)
        exit(1)


def download_apk():
    """使用 httpx 流式下载或复用APK"""
    version = get_version()
    apk_file_path = os.path.join(APK_DIR, f"tsk_dmm_{version}.apk")
    os.makedirs(APK_DIR, exist_ok=True)

    if os.path.exists(apk_file_path):
        print(f"[+] 使用已缓存的APK: {os.path.basename(apk_file_path)}")
        return apk_file_path

    print(f"[*] 正在下载版本 {version} 的APK...")
    try:
        with httpx.stream("GET", APK_URL, timeout=300.0) as response:
            response.raise_for_status()
            with open(apk_file_path, "wb") as f:
                for chunk in response.iter_bytes():
                    f.write(chunk)

        print("[+] 下载完成。")
        return apk_file_path
    except httpx.HTTPStatusError as e:
        print(f"[!] 下载APK失败 (HTTP错误): {e.response.status_code} - {e.request.url}")
        if os.path.exists(apk_file_path):
            os.remove(apk_file_path)  # 清理不完整的文件
        exit(1)
    except Exception as e:
        print(f"[!] 下载APK失败 (其他错误): {e}")
        if os.path.exists(apk_file_path):
            os.remove(apk_file_path)
        exit(1)


def sign_apk(apk_path):
    """根据环境选择签名方式"""
    is_github_actions = os.getenv("GITHUB_ACTIONS") == "true"

    if is_github_actions:
        print("[*] GitHub Actions 环境: 使用Base64密钥签名...")
        keystore_b64 = os.getenv("KEYSTORE_B64")
        keystore_pass = os.getenv("KEYSTORE_PASS")
        if not all([keystore_b64, keystore_pass]):
            raise ValueError(
                "在Actions环境中, KEYSTORE_B64和KEYSTORE_PASS secrets必须设置。"
            )

        with open("ci.keystore", "wb") as f:
            f.write(base64.b64decode(keystore_b64))

        signer_path = (
            f"{os.getenv('ANDROID_HOME')}/build-tools/{build_tools_version}/apksigner"
        )
        cmd = [
            signer_path,
            "sign",
            "--ks",
            "ci.keystore",
            "--ks-pass",
            f"pass:{keystore_pass}",
            "--ks-key-alias",
            KEYSTORE_ALIAS,
            apk_path,
        ]
        run_cmd(cmd)
        os.remove("ci.keystore")
    else:
        print("[*] 本地环境: 使用文件密钥和.env密码签名...")
        keystore_pass = os.getenv("KEYSTORE_PASS")
        if not os.path.exists(KEYSTORE_FILE) or not keystore_pass:
            raise FileNotFoundError(
                f"请确保 '{KEYSTORE_FILE}' 存在并且在 .env 文件中设置了 KEYSTORE_PASS。"
            )

        cmd = [
            "apksigner",
            "sign",
            "--ks",
            KEYSTORE_FILE,
            "--ks-pass",
            f"pass:{keystore_pass}",
            "--ks-key-alias",
            KEYSTORE_ALIAS,
            apk_path,
        ]
        run_cmd(cmd)

    print("[+] 签名成功。")


def process_apk(apk_file):
    """主处理流程：解包、注入、重打包、签名"""
    apk_filename = f"tsk_{get_version()}.apk"
    final_apk_path = os.path.join(DIST_DIR, apk_filename)
    os.makedirs(DIST_DIR, exist_ok=True)

    # 1. 解包
    if os.path.exists(BUILD_TEMP_DIR):
        shutil.rmtree(BUILD_TEMP_DIR)
    run_cmd(["apktool", "d", "-f", "-r", apk_file, "-o", BUILD_TEMP_DIR])

    # 2. 注入so
    lib_dir = os.path.join(BUILD_TEMP_DIR, "lib/arm64-v8a")
    os.makedirs(lib_dir, exist_ok=True)
    print("[*] 开始注入so文件...")
    for so_filename in os.listdir(LIB_DIR):
        if so_filename.endswith(".so"):
            source_file = os.path.join(LIB_DIR, so_filename)
            destination_file = os.path.join(lib_dir, so_filename)
            print(f"    -> 正在复制: {so_filename}")
            shutil.copy(source_file, destination_file)

    print("[+] so 注入成功。")

    # 3. Smali Patch
    smali_path_pattern = os.path.join(
        BUILD_TEMP_DIR, "smali*/com/unity3d/player/UnityPlayerActivity.smali"
    )
    smali_files = glob.glob(smali_path_pattern)
    if not smali_files:
        print("[!] 警告: 未找到 UnityPlayerActivity.smali, 注入可能失败。")
        return

    smali_path = smali_files[0]
    with open(smali_path, "r+", encoding="utf-8") as file:
        text = file.read()
        patch = (
            "invoke-direct {p0}, Landroid/app/Activity;-><init>()V\n\n"
            '    const-string v0, "tskmod"\n'
            "    invoke-static {v0}, Ljava/lang/System;->loadLibrary(Ljava/lang/String;)V"
        )
        if 'const-string v0, "tskmod"' not in text:
            text = text.replace(
                "invoke-direct {p0}, Landroid/app/Activity;-><init>()V", patch, 1
            )
            file.seek(0)
            file.write(text)
            file.truncate()
            print("[+] Smali Patch 成功。")

    # 4. 重打包
    run_cmd(["apktool", "b", BUILD_TEMP_DIR, "-o", final_apk_path])

    # 5. 签名
    sign_apk(final_apk_path)

    # 6. 清理
    shutil.rmtree(BUILD_TEMP_DIR)
    print(f"\n[SUCCESS] 构建完成: {final_apk_path}")


def main():
    print("--- TSK 数据抓取工具构建脚本 ---")
    original_apk = download_apk()
    process_apk(original_apk)


if __name__ == "__main__":
    main()
