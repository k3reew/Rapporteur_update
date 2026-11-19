import { useState } from 'react';
import { request, getJSON } from './api';
import { useResource } from './useResource';
import { useUnsavedChanges } from './useUnsavedChanges';

type Department = { id: string; version: number; name: string; original_name: string; pages: { page: number; text: string }[] };
export function Departments() {
  const { data, setData: setItems, error: loadError } = useResource<Department[]>('/api/v1/departments');
  const items = data ?? [];
  const [selected, setSelected] = useState('');
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const selectedName = items.find(item => item.id === selected)?.name ?? '';
  useUnsavedChanges(busy || name !== selectedName);
  async function upload(file: File) {
    setBusy(true); setError('');
    try {
      const params = new URLSearchParams({ name, filename: file.name });
      if (selected) params.set('department_id', selected);
      await request(`/api/v1/departments?${params}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file });
      setItems(await getJSON<Department[]>('/api/v1/departments'));
      setName(''); setSelected('');
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="card"><h2>Отделы и положения</h2>
    <p className="muted">Положения помогают определить, каким отделам интересен доклад. Рекомендации проверяются вместе со сводкой.</p>
    <details className="department-upload"><summary>Добавить отдел или обновить положение</summary><div className="fields">
    {(error || loadError) && <p role="alert">{error || loadError}</p>}
    <label className="field">Отдел<select value={selected} onChange={e => {
      setSelected(e.target.value); setName(items.find(d => d.id === e.target.value)?.name ?? '');
    }}><option value="">Новый отдел</option>{items.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}</select></label>
    <label className="field">Название отдела<input value={name} onChange={e => setName(e.target.value)} /></label>
    <label className="field">{busy ? 'Распознавание положения…' : 'Загрузить положение / новую версию'}<input type="file" accept=".doc,.docx,.pdf,.txt,.md"
      disabled={busy || !name.trim()} onChange={e => { const file = e.target.files?.[0]; e.target.value = ''; if (file) void upload(file); }} /></label>
    </div></details>
    {!data && !loadError && <p className="loading">Загрузка отделов…</p>}
    {data && !items.length && <p className="empty-state">Положения ещё не загружены. Добавьте первый отдел.</p>}
    <div className="department-list">{items.map(d => <details key={d.id}><summary>{d.name} · версия {d.version} · {d.original_name}</summary>
      {d.pages.map(p => <div key={p.page}><h3>Страница {p.page}</h3><pre style={{ whiteSpace: 'pre-wrap' }}>{p.text}</pre></div>)}
    </details>)}</div>
  </section>;
}

type Item = { department_id: string; department_version: number; reason: string; claim_ids: string[]; paragraph_ids: string[] };
type Result = { version: number; items: Item[]; reviewed: boolean; stale: boolean; job?: { job: string; error: string | null } | null };
export function Recommendations({ talkId }: { talkId: string }) {
  const url = `/api/v1/talks/${talkId}/recommendations`;
  const { data: result, setData: setResult, error: loadError } = useResource<Result>(url, 5000);
  const { data: deps = [] } = useResource<Department[]>('/api/v1/departments');
  const [draft, setDraft] = useState<{ version: number; items: Item[] }>();
  useUnsavedChanges(!!draft);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  async function act(review: boolean) {
    setError(''); setBusy(true);
    try {
      await request(url, { method: review ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' },
        ...(review ? { body: JSON.stringify({ version: draft?.version ?? result?.version, items: draft?.items ?? result?.items }) } : {}) });
      setResult(await (await request(url)).json()); setDraft(undefined);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  const items = draft?.items ?? result?.items ?? [];
  function edit(items: Item[]) { setDraft(previous => ({ version: previous?.version ?? result?.version ?? 0, items })); }
  const running = result?.job?.job === 'queued' || result?.job?.job === 'running';
  return <section className="card"><h3>Интерес отделов</h3>
    <button disabled={busy || running || !!draft} onClick={() => void act(false)}>Рассчитать рекомендации</button>
    {running && <p role="status">Расчет выполняется…</p>}
    {result?.job?.job === 'paused' && <p className="notice">Расчёт приостановлен после перезапуска приложения. Нажмите «Рассчитать рекомендации», чтобы продолжить.</p>}
    {(error || loadError || result?.job?.error) && <p role="alert">{error || loadError || result?.job?.error}</p>}
    {result?.stale && <p role="alert">Сводка или положения изменились. Рассчитайте рекомендации заново.</p>}
    {!!result?.version && !items.length && <p>Подходящих отделов не найдено.</p>}
    {items.map((item, i) => {
      const dep = deps.find(d => d.id === item.department_id);
      return <article key={i}><h4>{dep?.name ?? item.department_id} · положение v{item.department_version}</h4>
        <label className="field">Обоснование<textarea value={item.reason} disabled={busy || running} onChange={e => edit(items.map((r, j) => j === i ? { ...r, reason: e.target.value } : r))} /></label>
        <p>Факты доклада: {item.claim_ids.join(', ')}. Пункты: {item.paragraph_ids.join(', ')}.</p>
        <details><summary>Основания в положении</summary>{item.paragraph_ids.map(key => {
          const [page, part] = key.slice(1).split('.').map(Number);
          return <p key={key}>{key}: {dep?.pages.find(p => p.page === page)?.text.slice((part - 1) * 2000, part * 2000)}</p>;
        })}</details>
        <button disabled={busy || running} onClick={() => edit(items.filter((_, j) => j !== i))}>Убрать рекомендацию</button>
      </article>;
    })}
    {draft && <p className="notice">Есть несохранённые изменения. <button onClick={() => setDraft(undefined)}>Отменить правки</button></p>}
    {result?.reviewed && !draft && <p className="text-ok">Рекомендации подтверждены.</p>}
    {!!result?.version && <button disabled={busy || running || result.stale || (result.reviewed && !draft)} onClick={() => void act(true)}>Подтвердить рекомендации</button>}
  </section>;
}
