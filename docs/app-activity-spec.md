# VII app内置「最近活动」感知 — spec

## 背景
现状：哈娅手机上的MacroDroid，每次切换app（微信/QQ/小红书等）时调用
`GET /api/dream/events?type=app.{应用名}&value=在用{应用名}`，写入`dream_events`表，
被`build_system()`注入system prompt的"## 哈娅最近的活动"部分。

目标：VII这个Capacitor app自己实现这个上报，逐步替代/补充MacroDroid，
**后端完全不用改**，沿用同一个接口、同一种数据格式。

## 实现方式：UsageStatsManager（不是无障碍服务）

理由：权限申请友好（系统设置里"使用情况访问"，授权一次即可），
不需要常驻后台无障碍服务，延迟在我们的场景里完全可接受。

### 步骤

1. **写一个Capacitor原生插件**（Android，Kotlin/Java）：
   - 暴露一个方法 `getForegroundApp()`，内部用
     `UsageStatsManager.queryEvents()` 或 `queryUsageStats(INTERVAL_DAILY, ...)`
     取最近一次 `MOVE_TO_FOREGROUND` 事件对应的包名。
   - 暴露一个方法 `hasUsageAccessPermission()` 检查权限是否已授予。
   - 暴露一个方法 `openUsageAccessSettings()`，跳转到
     `Settings.ACTION_USAGE_ACCESS_SETTINGS`，供首次引导用户授权。

2. **AndroidManifest.xml** 加权限声明：
   ```xml
   <uses-permission android:name="android.permission.PACKAGE_USAGE_STATS"
       tool:ignore="ProtectedPermissions"/>
   ```

3. **前端（VII的WebView里的JS）**：
   - app启动时检查 `hasUsageAccessPermission()`，没有则弹一次引导
     （类似"为了让我能更懂你，需要你去设置里给我一个小小的权限"）。
   - 每60秒（前台运行时）调用一次 `getForegroundApp()`，
     拿到包名后查下面的映射表，转成中文应用名。
   - 如果跟上一次结果不同，调用：
     `GET /api/dream/events?type=app.{中文名}&value=在用{中文名}`
     （接口本身已做5分钟内同type去重，不用前端额外节流太狠）

4. **包名→中文名映射表**（先覆盖常用的，后续按实际遇到的随时加）：
   | 包名 | 中文名 |
   |---|---|
   | com.tencent.mm | 微信 |
   | com.tencent.mobileqq | QQ |
   | com.xingin.xhs | 小红书 |
   | com.ss.android.ugc.aweme | 抖音 |
   | tv.danmaku.bili | bilibili |
   | com.taobao.taobao | 淘宝 |
   | com.eg.android.AlipayGphone | 支付宝 |
   | com.sina.weibo | 微博 |
   | （遇到未知包名）| 直接上报包名本身，或忽略不报 |

## 不需要做的事
- 不需要无障碍服务
- 不需要前台常驻通知/service（依赖app本身在前台运行时轮询即可，
  退到后台就不再上报——这本身也是一种"活动信号"：app不在前台=她在用别的app）
- 后端 `/api/dream/events`、`dream_events`表、`build_system()`注入逻辑——全部不用改

## 验证方式
1. 安装debug包，授权"使用情况访问"
2. 切到微信/小红书晃一晃，回到VII，等60秒
3. `sqlite3 /opt/frontend/memories.db "SELECT * FROM dream_events ORDER BY id DESC LIMIT 5;"`
   能看到新的 `app.微信` / `app.小红书` 记录即可
