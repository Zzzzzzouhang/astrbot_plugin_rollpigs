<div align="center">
    <h2>🐖 astrbot_plugin_rollpigs 🐖</h2>
    <p>今天是什么小猪 🐽</p>
</div>

> **本项目移植自 NoneBot 插件 [nonebot-plugin-rollpig](https://github.com/Bearlele/nonebot-plugin-rollpig)，将其适配为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件框架。**
> **感谢原作者 [Bearlele](https://github.com/Bearlele) 的出色工作。**

---

### ✨ 特性 ✨

- **今日小猪** - 抽取今天属于你的小猪类型，每个用户每天一只
- **随机小猪** - 从 PigHub 随机获取猪猪图，支持一次获取多张
- **找猪** - 根据关键词或图片 ID 搜索猪猪
- **云端资源同步** - 自动从云端同步最新小猪资源，无需更新插件即可获得新猪猪

---

### 🚀 安装方式 🚀

**推荐优先使用 AstrBot 插件市场的仓库安装功能：**

1. 打开 AstrBot 管理面板。
2. 进入 **插件市场** → **通过 GitHub 仓库安装**。
3. 输入以下仓库地址并点击安装：

```
https://github.com/Zzzzzzouhang/rollpigs
```

**手动安装：**

```bash
cd AstrBot/data/plugins
git clone https://github.com/Zzzzzzouhang/rollpigs.git astrbot_plugin_rollpigs
```

安装后在 WebUI 中启用插件即可。


---

### 🕹️ 使用方法 🕹️

| 命令 | 别名 | 说明 |
|------|------|------|
| `今日小猪` | 今天是什么小猪、本日小猪、当日小猪 | 抽取今天属于你的小猪 |
| `随机小猪 [数量]` | — | 从 PigHub 随机获取猪猪图，数量 1-20，默认 1 |
| `找猪 <关键词>` | 搜猪 | 按关键词搜索猪猪 |
| `找猪 id <ID>` | — | 按图片 ID 查找猪猪 |
| `同步小猪资源` | 刷新小猪图鉴 | 手动同步云端资源（仅管理员） |

> [!TIP]
> 以上命令均可在消息前加 `/` 触发，也可直接发送中文指令。

---

### ⚙️ 配置项 ⚙️

安装后在 AstrBot WebUI 的插件管理页面可配置以下选项：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 云端资源同步 | 开关 | 开启 | 是否启用云端小猪资源同步 |
| Manifest URL | 字符串 | `https://pig.felislab.cc/resources/rollpig/manifest.json` | 云端资源 manifest 地址 |
| 同步间隔 | 整数 | 24 | 定时同步间隔（小时），最小 1 |
| 同步超时 | 浮点数 | 10.0 | 单次网络请求超时（秒） |
| 最大文件大小 | 整数 | 10485760 | 单个资源文件最大字节数（默认 10MB） |

如需关闭云端同步，在 WebUI 中将「云端资源同步」关闭即可，插件将仅使用内置资源。

---

### ☁️ 云端资源同步 ☁️

插件默认会从云端同步小猪资源包（`pig.json` + 图片），用于在不更新插件代码的情况下获取新猪猪。

- 启动时自动尝试同步
- 按配置的间隔定时同步（默认每 24 小时）
- 同步失败时自动回退到内置资源，不影响使用
- 管理员可手动发送 `同步小猪资源` 强制同步

如需使用自己的资源站点，可在 WebUI 配置中将 Manifest URL 改为你自己的 `manifest.json` 地址。

---

### 📂 目录结构 📂

```
astrbot_plugin_rollpigs/
├── main.py                  # 插件主逻辑
├── metadata.yaml            # 插件元数据
├── _conf_schema.json        # 配置项定义
├── requirements.txt         # Python 依赖
├── README.md
├── LICENSE
├── resource/                # 内置资源（兜底）
│   ├── pig.json             # 猪格数据
│   ├── template.html        # 渲染模板
│   └── image/               # 猪猪图片
└── test_plugin/             # 测试脚本
    ├── conftest.py
    └── test_rollpig.py
```

运行时数据存储在 AstrBot 的 `data/plugin_data/astrbot_plugin_rollpigs/` 目录下：
- `records.json` — 用户每日抽取记录
- `resources/` — 云端同步缓存

---

### 🧪 测试 🧪

```bash
pip install pytest pytest-asyncio
python -m pytest test_plugin/test_rollpig.py -v
```

---

### 🐷 新增小猪 🐷

如需添加新猪猪到内置资源：

1. 在 `resource/pig.json` 中添加条目：
```json
{
    "id": "my-new-pig",
    "name": "新猪猪",
    "description": "这只猪猪的描述",
    "analysis": "这只猪猪的性格分析"
}
```

2. 将对应图片放入 `resource/image/` 目录，文件名与 `id` 一致（如 `my-new-pig.png`）
3. 支持图片格式：`png`, `jpg`, `jpeg`, `webp`, `gif`

> [!NOTE]
> 新增内置资源后需重新加载插件。也可以通过云端同步机制动态更新资源，无需重新部署插件。

---

### 🙏 鸣谢 🙏

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 跨平台 AI 对话代理框架
- [nonebot-plugin-rollpig](https://github.com/Bearlele/nonebot-plugin-rollpig) — 原 NoneBot 插件，本插件的功能来源
- [PigHub](https://pighub.top/) — 猪猪图片数据源
