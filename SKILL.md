---
name: muse-job-workflow
description: Deploy and run a private job-search workflow in Muse, with employer-site verification, a deduplicated application queue, draft preparation, and receipt monitoring.
---

# Muse 求职工作流

当用户把这个仓库发给你并要求部署时，在用户自己的 Muse 工作区执行下列步骤。简历、筛选配置、队列、证据和运行日志存放在用户的私有工作区。

1. 读取 [详细部署指令](DEPLOYMENT-PROMPT.md)、[部署手册](index.html) 和 [示例配置](config.example.json)。有终端时在私有工作区获取本仓库源码，复用已有副本。运行 `python3 -B scripts/bootstrap.py`，随后运行 `python3 -B -m unittest discover -s tests`。Bootstrap 会创建私有数据目录、配置和运行目录，重复运行会保留现有配置。无终端时，用现有文件和任务工具搭建等价结构，将本地脚本验收记为待验。
2. 一次性收集尚缺的求职地区、IANA 时区、岗位方向和级别、远程范围、材料位置及扫描频率。先完成不依赖回答的搭建。优先复用现成工具和任务，缺少搜索连接时再按手册接入 Agent Reach。
3. 建立岗位扫描、私有去重队列、单一草稿执行端和只读监督。扫描保存覆盖证据，到雇主官网核对 Req ID、完整 JD 和开放状态。按雇主和 Req ID 去重。先演练重复派发，再核验一个真实岗位并写入私有台账。调度启用前核对时区与已有任务，避免重复运行。
4. 部署时保持 `submitEnabled=false`。雇主登录、MFA 和工具授权都走用户的原生界面，使用自己的安全存储。仅在用户明确批准真实投递范围后启用提交。已有工作流的提交设置不由安装脚本改动。
5. 回报私有配置和队列的位置、扫描和监督任务 ID、下次运行时间、真实岗位验收证据和待授权项。只有确实创建的任务才填 ID。
