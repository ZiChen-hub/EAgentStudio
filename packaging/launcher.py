"""PyInstaller 入口：以包的方式导入桌面应用，避免相对导入失效。"""

from eagent_bridge.desktop import main

if __name__ == "__main__":
    main()
