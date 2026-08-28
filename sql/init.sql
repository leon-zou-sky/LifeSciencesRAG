-- =============================================
-- 生命科学 RAG PoC：源数据层（MySQL 是源，Milvus 是影子）
-- 容器首次初始化时自动执行
-- =============================================

CREATE DATABASE IF NOT EXISTS ls_rag DEFAULT CHARACTER SET utf8mb4;
USE ls_rag;

-- 文档主表：一份文档一行（说明书/论文/指南/病例报告通用）
CREATE TABLE IF NOT EXISTS documents (
    id INT PRIMARY KEY AUTO_INCREMENT,
    doc_type VARCHAR(16) NOT NULL COMMENT '说明书/论文/指南/病例报告',
    title VARCHAR(512) NOT NULL COMMENT '药品名/论文标题/病例号',
    authority VARCHAR(4) COMMENT '权威级 A/B/C',
    version VARCHAR(32) COMMENT '版本（说明书用）',
    status VARCHAR(16) DEFAULT '现行' COMMENT '现行/作废',
    source_meta JSON COMMENT '类型差异字段：论文{journal,authors,year,doi} 说明书{drug_name,批准文号} 病例{case_id}',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_type_title_version (doc_type, title(255), version),
    INDEX idx_type_status (doc_type, status)
) COMMENT='文档主表';

-- 文档块表：一份文档按章节拆成 N 块
CREATE TABLE IF NOT EXISTS document_chunks (
    id INT PRIMARY KEY AUTO_INCREMENT,
    document_id INT NOT NULL COMMENT '→ documents.id',
    section VARCHAR(64) COMMENT '章节名：适应症/摘要/方法/结果...',
    seq INT DEFAULT 0 COMMENT '章节在文档内的顺序',
    text TEXT COMMENT '块全文（含【标题】【章节】前缀，与入向量库一致）',
    embedding_updated_at DATETIME NULL COMMENT '向量同步标记：NULL=待同步',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_document (document_id),
    INDEX idx_sync (embedding_updated_at)
) COMMENT='文档块表';

-- 医学问询表（事件流：判重+人工确认后入库）
CREATE TABLE IF NOT EXISTS mi_inquiries (
    id INT PRIMARY KEY AUTO_INCREMENT,
    question_raw VARCHAR(512) NOT NULL COMMENT '医生原始问题（保留口语原文）',
    question_std VARCHAR(512) COMMENT '归一后标准问题（商品名→通用名）',
    answer TEXT COMMENT '答复内容（未答复为 NULL）',
    drugs VARCHAR(256) COMMENT '涉及药品通用名，逗号分隔',
    channel VARCHAR(16) COMMENT '来源渠道：热线/微信/代表转达',
    status VARCHAR(16) DEFAULT 'draft' COMMENT 'draft/approved（审核后才可被检索引用）',
    authority VARCHAR(4) DEFAULT 'C' COMMENT '初始C，审核通过升B',
    occurrence_count INT DEFAULT 1 COMMENT '重复问题计数',
    embedding_updated_at DATETIME NULL COMMENT '向量同步标记：NULL=待同步',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_question_std (question_std(255)),
    INDEX idx_sync (embedding_updated_at),
    INDEX idx_status (status)
) COMMENT='医学问询记录表';
