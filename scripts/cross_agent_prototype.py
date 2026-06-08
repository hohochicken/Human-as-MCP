"""跨 Agent 知识查询原型 —— 验证可行性。"""
import sqlite3, re
from datetime import datetime, timezone, timedelta

DB_PATH = "H:/Human/data/tasks.db"

def extract_keywords(text: str) -> list[str]:
    keywords = []
    uppercase = re.findall(r'\b[A-Z][A-Z0-9_]{2,}\b', text)
    keywords.extend([k.lower() for k in uppercase])
    tech_words = [
        'redis', 'mysql', 'postgres', 'nginx', 'docker',
        '密码', 'password', '地址', 'address', '端口', 'port',
        '配置', 'config', '环境', 'environment', '部署', 'deploy',
        '重启', 'restart', 'vue', 'react', 'router', '路由',
        'shader', '光照', '编译', 'build', '测试', 'test',
    ]
    text_lower = text.lower()
    for w in tech_words:
        if w in text_lower:
            keywords.append(w)
    return list(set(keywords))

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    rows = conn.execute("""
        SELECT task_id, agent_id, title, tool_name, result, completed_at 
        FROM tasks WHERE status='completed' AND result IS NOT NULL
        ORDER BY completed_at DESC
    """).fetchall()

    print(f'=== {len(rows)} 条已完成任务 ===')
    for r in rows:
        print(f'  [{r["agent_id"]}] {r["tool_name"]}: {r["title"][:80]}')
        result_preview = r["result"][:120] if r["result"] else "(空)"
        print(f'    结果: {result_preview}')
    print()

    test_questions = [
        ('Redis', 'redis'),
        ('shader/光照', 'shader'),
        ('配置/config', '配置'),
        ('部署/deploy', '部署'),
    ]

    print('=== 跨 Agent 知识匹配测试 ===')
    for topic, kw in test_questions:
        rows2 = conn.execute("""
            SELECT agent_id, title, tool_name, result, completed_at
            FROM tasks WHERE status='completed' AND result IS NOT NULL
            AND (title LIKE ? OR description LIKE ?)
            ORDER BY completed_at DESC
        """, (f'%{kw}%', f'%{kw}%')).fetchall()
        
        agents = list(set(r['agent_id'] for r in rows2))
        print(f'关键词 "{topic}": 匹配 {len(rows2)} 条，涉及 {len(agents)} 个 Agent: {agents}')
    
    # 关键测试：模拟新 Agent "codex" 查询，能否找到其他 Agent 的知识
    print()
    print('=== 模拟：codex 问 Redis 密码，能否找到其他 Agent 的经验？ ===')
    test_agent = "codex"
    test_question = "测试环境 Redis 连接密码是什么？"
    keywords = extract_keywords(test_question)
    print(f'问题: {test_question}')
    print(f'提取关键词: {keywords}')
    
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    found_any = False
    
    for kw in keywords:
        rows3 = conn.execute("""
            SELECT agent_id, title, result, completed_at
            FROM tasks WHERE status='completed' AND result IS NOT NULL
            AND agent_id != ?
            AND completed_at > ?
            AND (title LIKE ? OR description LIKE ?)
            ORDER BY completed_at DESC LIMIT 5
        """, (test_agent, cutoff, f'%{kw}%', f'%{kw}%')).fetchall()
        
        for row in rows3:
            found_any = True
            print(f'  ✅ 匹配: [{row["agent_id"]}] {row["title"][:60]}')
            print(f'     答案: {row["result"][:150] if row["result"] else "(空)"}')
    
    if not found_any:
        print('  ❌ 未找到跨 Agent 知识匹配')
    
    # 总结
    all_agents = conn.execute("SELECT DISTINCT agent_id FROM tasks WHERE status='completed'").fetchall()
    print(f'\n=== 总计 {len(all_agents)} 个 Agent 有已完成任务 ===')
    for a in all_agents:
        cnt = conn.execute("SELECT COUNT(*) FROM tasks WHERE agent_id=? AND status='completed'", (a['agent_id'],)).fetchone()[0]
        print(f'  {a["agent_id"]}: {cnt} 条已完成')
    
    conn.close()
    return found_any

if __name__ == "__main__":
    main()
