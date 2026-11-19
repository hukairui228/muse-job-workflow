# Muse 求职工作流

把下面这句话发到自己的 [Muse](https://muse.ai/) 对话里。

> 请在我的私有工作区部署 https://github.com/hukairui228/muse-job-workflow 。读取仓库的 SKILL.md，完成初始化和首轮只读验收，再告诉我需要补的设置或授权。保持真实提交关闭。

Muse 会直接从 GitHub 读取模板。首次部署时，由你提供求职偏好，并在原生界面完成所需登录和授权。流程包含岗位发现、官网核验、私有申请队列、草稿和回执监督。

[Muse Skill 入口](SKILL.md) · [HTML 手册](index.html) · [详细部署指令](DEPLOYMENT-PROMPT.md) · [配置示例](config.example.json)

## 在终端初始化

```sh
git clone https://github.com/hukairui228/muse-job-workflow.git
cd muse-job-workflow
python3 -B scripts/bootstrap.py
python3 -B -m unittest discover -s tests
```

初始化脚本在私有数据目录准备配置和运行目录。后续连接、调度和首轮验收由 Muse 按 [SKILL.md](SKILL.md) 完成。
