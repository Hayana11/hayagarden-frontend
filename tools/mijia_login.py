#!/usr/bin/env python3
"""
米家扫码登录脚本（后台运行）。
  python3.11 /opt/frontend/tools/mijia_login.py
流程：
  1. 生成二维码图片 -> /opt/frontend/static/qrcode.png
  2. 打开 /setup/mijia 页面用米家 App 扫码
  3. 扫码完成后认证信息自动保存到 /opt/frontend/.mijia_auth
"""
import sys
QR_PNG    = '/opt/frontend/static/qrcode.png'
AUTH_PATH = '/opt/frontend/.mijia_auth'

# 截获库内部的二维码打印，转存成 PNG 供前端展示
import qrcode
from mijiaAPI import apis

def _save_qr(loginurl, box_size=10):
    try:
        img = qrcode.make(loginurl)
        img.save(QR_PNG)
        print(f"[mijia_login] 二维码已保存到 {QR_PNG}", flush=True)
    except Exception as e:
        print(f"[mijia_login] 保存二维码失败: {e}", flush=True)

apis.mijiaAPI._print_qr = staticmethod(_save_qr)

def main():
    from mijiaAPI import mijiaAPI
    print("[mijia_login] 开始登录流程，请打开 /setup/mijia 用米家 App 扫码…", flush=True)
    api = mijiaAPI(auth_data_path=AUTH_PATH)
    try:
        api.QRlogin()  # 阻塞，等待扫码（最长约120秒）
    except Exception as e:
        print(f"[mijia_login] 登录失败/超时: {e}", flush=True)
        sys.exit(1)
    print(f"[mijia_login] 登录成功，认证信息已保存到 {AUTH_PATH}", flush=True)
    # 顺手列一下设备，方便确认次卧灯的 did
    try:
        for d in api.get_devices_list():
            print(f"  - {d.get('name')} | did={d.get('did')} | model={d.get('model')}", flush=True)
    except Exception as e:
        print(f"[mijia_login] 列设备失败: {e}", flush=True)

if __name__ == '__main__':
    main()
