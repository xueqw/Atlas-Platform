## ADDED Requirements

### Requirement: Short-term memory
系统 SHALL 将 Agent 对话短期记忆保存在 Redis，并按工作区、用户、Agent 和会话隔离，同时设置 TTL。

#### Scenario: Short-term memory expires
- **WHEN** 短期记忆超过配置的 TTL
- **THEN** 系统不再将该记忆返回给 Agent

### Requirement: Long-term memory consent and isolation
系统 SHALL 仅通过显式记忆写入操作保存长期记忆，并记录工作区、用户、Agent、内容、分类和过期时间。

#### Scenario: Cross-user read denied
- **WHEN** 用户请求读取同工作区另一用户的私有长期记忆
- **THEN** 系统返回拒绝或空结果

### Requirement: Memory deletion
系统 SHALL 允许用户删除自己的单条或全部 Agent 记忆，并使删除结果立即生效。

#### Scenario: Delete all agent memories
- **WHEN** 用户确认删除某 Agent 的全部个人记忆
- **THEN** 系统删除对应长期记录和 Redis 短期记录
