## ADDED Requirements

### Requirement: Docker isolated execution
代码型 Agent SHALL 在一次性 Docker 容器中执行，默认禁止网络，限制 CPU、内存、PID、写入范围和执行时间，并且不得继承 Atlas 主服务密钥。

#### Scenario: Infinite loop
- **WHEN** Agent 代码超过执行时间限制
- **THEN** 系统终止容器并返回超时错误，不影响 Atlas 主服务

#### Scenario: Network request
- **WHEN** 未获网络许可的 Agent 代码尝试访问外网
- **THEN** 容器网络策略阻断请求

### Requirement: Sandboxed HTML preview
HTML 预览 SHALL 在无同源权限的 sandbox iframe 中运行，并通过 CSP 阻断外部网络、插件、父页面导航和登录态访问。

#### Scenario: Preview script reads parent
- **WHEN** 预览脚本尝试读取 Atlas 父页面 DOM 或 Cookie
- **THEN** 浏览器同源和 sandbox 策略阻止访问
