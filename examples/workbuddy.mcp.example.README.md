# WorkBuddy MCP 配置

运行初始化命令后，部署目录中的 `workbuddy.mcp.example.json` 会包含当前 Python 解释器及 `bridge.json` 的绝对路径。将其中 `workbuddy-pythongo` 节点合并进 WorkBuddy MCP 配置，不要覆盖其他 MCP Server。

启动顺序为：Bridge Worker → 无限易中的 PythonGO Adapter → WorkBuddy。若 `bridge.json.port` 发生变化，无需手工写 endpoint；MCP 前端会从同一配置读取回环地址和 token 文件。

配置中不得加入真实投资者账号、HMAC key、Worker token、已签名 Profile 或 LIVE 授权内容。
