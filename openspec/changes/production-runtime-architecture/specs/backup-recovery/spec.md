## ADDED Requirements

### Requirement: Daily recoverable backup
系统 SHALL 每日备份 PostgreSQL、MinIO 对象、Agent 持久化目录和非敏感部署配置，并按配置周期清理旧备份。

#### Scenario: Backup succeeds
- **WHEN** 定时备份任务完成
- **THEN** 系统生成带时间戳的备份目录、校验清单和成功标记

### Requirement: Restore verification
系统 SHALL 提供恢复脚本和只校验模式，且恢复操作必须显式确认目标环境。

#### Scenario: Verification only
- **WHEN** 运维执行备份校验而未传入恢复确认参数
- **THEN** 系统只检查文件和校验和，不覆盖当前数据
