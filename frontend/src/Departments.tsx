import { useState } from 'react';
import { request, getJSON } from './api';
import { useResource } from './useResource';
import { useUnsavedChanges } from './useUnsavedChanges';

export type Department = {
  id: string; version: number; name: string; original_name?: string | null; filename?: string | null;
  description?: string; functions?: string; pages: { page: number; text: string }[];
};

export function departmentReady(department: Pick<Department, 'pages' | 'description' | 'functions'>) {
  return !!(department.description?.trim() || department.functions?.trim() || department.pages?.some(page => page.text.trim()));
}

export function Departments() {
  const url = '/api/v1/departments';
  const { data, setData: setItems, error: loadError } = useResource<Department[]>(url);
  const items = data ?? [];
  const anyReady = items.some(departmentReady);
  const [selected, setSelected] = useState('');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [functions, setFunctions] = useState('');
  const [file, setFile] = useState<File>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const department = items.find(item => item.id === selected);
  const fieldsDirty = name !== (department?.name ?? '') || description !== (department?.description ?? '')
    || functions !== (department?.functions ?? '');
  useUnsavedChanges(busy || fieldsDirty || !!file);

  function choose(id: string) {
    const item = items.find(entry => entry.id === id);
    setSelected(id);
    setName(item?.name ?? '');
    setDescription(item?.description ?? '');
    setFunctions(item?.functions ?? '');
    setFile(undefined);
    setError('');
  }

  async function save() {
    setBusy(true); setError('');
    let list = items;
    let id = selected;
    function replace(item: Department) {
      list = [...list.filter(entry => entry.id !== item.id), item];
      setItems(list);
    }
    try {
      if (fieldsDirty && (id || description.trim() || functions.trim())) {
        const saved: Department = await (await request(url, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name.trim(), description: description.trim(), functions: functions.trim(), ...(id ? { department_id: id } : {}) }),
        })).json();
        id = saved.id;
        replace(saved);
        setSelected(id);
        setName(saved.name);
        setDescription(saved.description ?? '');
        setFunctions(saved.functions ?? '');
      }
      if (file) {
        const params = new URLSearchParams({ name: name.trim(), filename: file.name });
        if (id) params.set('department_id', id);
        replace(await (await request(`${url}?${params}`, {
          method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file,
        })).json());
      }
      choose('');
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function remove(item: Department) {
    if (!window.confirm(`Удалить отдел «${item.name}» со всеми версиями положения? Отбор и рекомендации по нему станут устаревшими. Отменить удаление нельзя.`)) return;
    setBusy(true); setError('');
    try {
      await request(`/api/v1/departments/${item.id}`, { method: 'DELETE' });
      setItems(await getJSON<Department[]>('/api/v1/departments'));
      if (selected === item.id) choose('');
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="card"><h2>Отделы и положения</h2>
    <p className="muted">Опишите задачи отдела или приложите положение. Эти сведения нужны для отбора докладов.</p>
    {anyReady && <div className="next-action">
      <p>Сведения об отделах готовы для анализа конференции.</p>
      <a className="primary action-link" href="#/">Перейти к материалам</a>
    </div>}
    {busy && <p role="status">Обновляем список отделов…</p>}
    {(error || loadError) && <p role="alert">{error || loadError}</p>}
    <details className="department-upload" open={data?.length === 0 || undefined}>
      <summary>Добавить или изменить отдел</summary>
      <form className="fields" onSubmit={event => { event.preventDefault(); void save(); }}>
        <fieldset disabled={busy}>
          <label className="field">Отдел<select value={selected} onChange={e => choose(e.target.value)}>
            <option value="">Новый отдел</option>{items.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select></label>
          <label className="field">Название отдела<input value={name} required onChange={e => setName(e.target.value)} /></label>
          <label className="field">Описание<textarea rows={3} value={description} onChange={e => setDescription(e.target.value)} /></label>
          <label className="field">Функции<textarea rows={4} value={functions} onChange={e => setFunctions(e.target.value)} /></label>
          <label className="field">Положение (необязательно)<input type="file" accept=".doc,.docx,.pdf,.txt,.md"
            onChange={e => { setFile(e.target.files?.[0]); e.target.value = ''; }} /></label>
          {file && <p className="row">{file.name} <button className="link" type="button" onClick={() => setFile(undefined)}>Снять вложение</button></p>}
          <button className="primary" disabled={!name.trim() || (!fieldsDirty && !file)
            || !(description.trim() || functions.trim() || file || department?.pages?.length)}>Сохранить отдел</button>
        </fieldset>
      </form>
    </details>
    {!data && !loadError && <p className="loading">Загрузка отделов…</p>}
    {data && !items.length && <p className="empty-state">Добавьте первый отдел.</p>}
    <div className="department-list">{items.map(d => <details key={d.id}><summary>{d.name} · версия {d.version}{(d.filename || d.original_name) && ` · ${d.filename || d.original_name}`}</summary>
      {d.description && <div><h3>Описание</h3><p className="preserve-lines">{d.description}</p></div>}
      {d.functions && <div><h3>Функции</h3><p className="preserve-lines">{d.functions}</p></div>}
      {!departmentReady(d) && <p className="notice">Добавьте описание, функции или читаемое положение.</p>}
      {(d.filename || d.original_name) && d.pages.map(p => <div key={p.page}><h3>Страница {p.page}</h3><pre style={{ whiteSpace: 'pre-wrap' }}>{p.text}</pre></div>)}
      <div className="row"><button className="danger" disabled={busy} onClick={() => void remove(d)}>Удалить отдел</button></div>
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
