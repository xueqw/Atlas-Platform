## ADDED Requirements

### Requirement: Production persistence boundaries
系统在生产模式下 SHALL 使用 PostgreSQL 保存业务数据、MinIO 保存知识原文件和 Agent 代码归档、Redis 保存带过期时间的临时状态。

#### Scenario: Knowledge document upload
- **WHEN** 工作区成员上传一个合法知识文档
- **THEN** 系统先按工作区前缀保存原文件，再提交文档元数据和分块记录

#### Scenario: Tenant isolation
- **WHEN** 一个工作区请求另一个工作区的对象或状态
- **THEN** 系统拒绝访问且不返回对象内容

### Requirement: Dependency health visibility
系统 SHALL 在健康接口中分别报告数据库、Redis、对象存储和 Docker Runner 状态，且不泄露凭据。

#### Scenario: Redis unavailable
- **WHEN** Redis 连接失败
- **THEN** 健康接口将 Redis 标记为不可用并保持其他依赖的独立状态

### Requirement: Secret file injection
系统 SHALL 支持从环境变量或权限受限的挂载文件读取生产密钥，且不得在日志和 API 响应中返回密钥值。

#### Scenario: Secret file configured
- **WHEN** 配置了一个密钥的 `_FILE` 路径
- **THEN** 系统读取文件内容并优先于普通环境变量使用
