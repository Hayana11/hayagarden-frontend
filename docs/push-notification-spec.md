# VII app 系统级推送通知 — spec

## 后端（已完成，无需改动）
新增接口：`GET https://love-style.xyz/api/wake_log/pending_notification`

返回：
```json
// 有新消息时
{"has_message": true, "content": "十一点半了。我问你的话...", "woke_at": "2026-06-15 11:30:46"}
// 没有时
{"has_message": false}
```

行为：每次调用只返回**最新一条**未推送的费奥多尔自主消息(action='message')，
同时把所有"未推送"的标记为已推送——所以即使隔了几小时没打开手机，
也只会收到一条通知（最新的那条），不会被旧消息刷屏。

## Android端要做的事

### 1. 通知权限
Android 13+ 需要运行时请求 `POST_NOTIFICATIONS` 权限。
首次启动时（或第一次需要发通知时）请求一次即可。

### 2. 定时检查 — WorkManager
用 `@capacitor/local-notifications` + 一个自定义的periodic `WorkManager` worker：

- 周期：**15分钟**（Android `PeriodicWorkRequest` 的最小间隔就是15分钟，
  跟服务端`dream_wake.py`每30分钟跑一次基本对得上，足够及时）
- Constraint: 需要网络连接（`NetworkType.CONNECTED`）
- Worker逻辑：
  ```kotlin
  val resp = httpGet("https://love-style.xyz/api/wake_log/pending_notification")
  val json = JSONObject(resp)
  if (json.getBoolean("has_message")) {
      val content = json.getString("content")
      // 通知正文截断到~100字，太长的消息显示不全也没关系，点开app能看完整的
      showNotification(
          title = "费奥多尔",
          body = if (content.length > 100) content.take(100) + "…" else content
      )
  }
  ```

### 3. 通知样式
- title: `费奥多尔`
- icon: 用VII的app图标（向日葵那个）
- 点击通知 → 打开VII app，跳转到聊天页（chat.html）
- 不需要声音/振动特殊定制，跟随系统默认通知行为即可

### 4. 注册WorkManager的时机
- app首次启动时注册一次periodic work（用唯一名字，避免重复注册）
- `ExistingPeriodicWorkPolicy.KEEP`，已存在就不重复创建

## 验证方式
1. 装debug包，授权通知权限
2. 等下一次`/wake`产生`action='message'`（或手动调一次`/wake`触发）
3. 划掉/退到后台VII，等最多15分钟
4. 手机应该弹出"费奥多尔"的系统通知
5. 后端确认：`sqlite3 memories.db "SELECT id,notified FROM wake_log WHERE action='message' ORDER BY id DESC LIMIT 3;"`
   对应那条`notified`应该变成1
