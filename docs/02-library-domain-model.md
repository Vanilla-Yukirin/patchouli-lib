# 领域模型：Library、Section、Book、Page 与 Revision

## 概览

```text
Library
├── Section
│   └── Book
│       └── Page
│           └── Revision
├── Tag
├── Shelf
├── Source
└── Derived Fact
```

## 知识库（Library）

Library 表示一个逻辑知识服务。首个实现可以让每次部署只提供一个 Library，但数据
模型不应阻碍未来的导出或多 Library 工具。

## 分区（Section）

Section 是稳定的策略和导航边界，例如项目、研究、日志或对话归档。

- 每个 Book 只属于一个 Section。
- 查询通常应先选择 Section，再执行搜索。
- Section 可以定义保留、授权、索引和整理策略。
- Section 是持久分类，不是临时搜索结果。

## 书籍（Book）

Book 是稳定的上下文容器。

- Page 在任意时刻只属于一个 Book。
- 创建 Page 前，目标 Book 必须已经存在。
- 移动 Page 只改变其 Book 归属，不复制或重写 Revision 历史。
- 创建 Book 的成本较低，但已有 Book 的身份保持稳定。
- 拆分和合并是明确、可审计的工作流。

## 页面（Page）

Page 是可单独寻址的最小文档。建议字段包括：

- 稳定 ID 和便于人类阅读的标题；
- 所属 Book 和内容类型；
- 生命周期状态和当前 Revision；
- 摘要、Tag 和 Source 元数据；
- 创建与更新时间戳。

## 版本（Revision）

Revision 是不可变的 Page 正文及其变更元数据。Page 指向当前 Revision；旧 Revision
仍可通过明确的历史操作访问。详见
[03-page-revision-and-history.md](03-page-revision-and-history.md)。

## 标签（Tag）

Tag 是多对多的检索提示，不能替代 Section 或 Book 归属。除非访问控制设计明确评估
过，否则不得把 Tag 当作授权机制。

Tag 推荐可以自动生成。应用一个推荐 Tag 是另外一次可审计写入。

## 书架（Shelf）

Shelf 是保存下来的 Book 投影视图，不拥有任何源内容。删除 Shelf 不能删除 Book 或
Page。Shelf 应由用户手动维护还是自动维护，仍是开放的产品问题。

## 来源（Source）

Source 记录描述来源，例如原始 URL、导入标识符或抓取时间。Source 元数据不得暗示
用户已经获得复制或再分发所引用材料的许可。

## 派生事实（Derived Fact）

Derived Fact 是从一个或多个 Page 中提取的简短、可重新生成的陈述。它保留明确的
来源链接，绝不会成为其源 Revision 的规范替代品。

## 关系模型草图

```text
sections(id, name, description, policy, created_at, updated_at)
books(id, section_id, name, summary, created_at, updated_at)
pages(id, book_id, stable_id, title, type, status,
      summary, current_revision_id, created_at, updated_at, deleted_at)
revisions(id, page_id, number, content_md, message,
          actor_id, created_at)
tags(id, name)
page_tags(page_id, tag_id)
sources(id, page_id, kind, locator, captured_at)
facts(id, content, generation, created_at)
fact_sources(fact_id, page_id, revision_id)
shelves(id, name, query_spec, updated_at)
audit_events(id, actor_id, action, resource_type, resource_id, created_at)
```

这份草图不是迁移或实现契约。数据类型、约束和索引仍需通过公开存储提案确定。
