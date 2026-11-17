import { useRef, useState } from 'react';
import { request } from './api';
import { kinds } from './materialLabels';
import type { Review } from './materialTypes';
import { useResource } from './useResource';
import { useUnsavedChanges } from './useUnsavedChanges';

export function Materials({ conferenceId, onPrepared }: { conferenceId: string; onPrepared: () => void }) {
  const base = `/api/v1/conferences/${conferenceId}`;
  const { data: review, error, refresh } = useResource<Review>(`${base}/materials`);
  const filesInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState('');
  const allowNavigation = useUnsavedChanges(!!busy);
  const [status, setStatus] = useState('');
  const [errors, setErrors] = useState<string[]>([]);
  const files = review?.files ?? [];

  async function upload(input: HTMLInputElement) {
    const selected = Array.from(input.files ?? []);
    input.value = '';
    if (!selected.length) return;
    const failures: string[] = [];
    let uploaded = 0, duplicates = 0;
    setErrors([]); setStatus('');
    const entries = selected.map(file => ({ file, path: file.webkitRelativePath ? file.webkitRelativePath.split('/').slice(1).join('/') : file.name }))
      .filter(item => !(item.file.webkitRelativePath && item.path === 'conference.yaml'))
      .sort((a, b) => Number(/\.(pptx|pdf)$/i.test(b.path)) - Number(/\.(pptx|pdf)$/i.test(a.path)) || a.path.localeCompare(b.path, 'ru'));
    try {
      for (const [index, { file, path }] of entries.entries()) {
        setBusy(`Загрузка ${index + 1} из ${entries.length}`);
        try {
          const response = await request(`${base}/materials?${new URLSearchParams({ path })}`, {
            method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file,
          });
          if (response.status === 200) duplicates++; else uploaded++;
        } catch (reason) { failures.push(`${path} — ${(reason as Error).message}`); }
      }
      refresh();
      setStatus(`Загружено ${uploaded}, уже было в конференции ${duplicates}.`);
    } finally { setBusy(''); setErrors(failures); }
  }

  async function prepare() {
    setBusy('Подготовка докладов…'); setErrors([]);
    try { await request(`${base}/preparation`, { method: 'POST' }); allowNavigation(); onPrepared(); }
    catch (reason) { setErrors([(reason as Error).message]); }
    finally { setBusy(''); }
  }

  return <section className="workflow-page" aria-labelledby="materials-title" aria-busy={!!busy}>
    <h2 id="materials-title">Загрузите материалы конференции</h2>
    <p className="muted">Презентации, тексты выступлений, фото, аудио или видео.</p>
    <div className="upload-zone">
      <div><h3>Добавьте всё, что у вас есть</h3><p className="muted">Можно выбрать несколько файлов или папку целиком.</p></div>
      <div className="row"><button disabled={!!busy} onClick={() => filesInput.current?.click()}>Выбрать файлы</button>
        <button className="link" disabled={!!busy} onClick={() => folderInput.current?.click()}>Выбрать папку</button></div>
      <input className="sr-only" tabIndex={-1} aria-label="Отдельные файлы" type="file" multiple ref={filesInput} disabled={!!busy} onChange={e => void upload(e.currentTarget)} />
      <input className="sr-only" tabIndex={-1} aria-label="Папка конференции" type="file" ref={input => { folderInput.current = input; input?.setAttribute('webkitdirectory', ''); }} disabled={!!busy} onChange={e => void upload(e.currentTarget)} />
    </div>
    <p role="status">{busy || status}</p>
    {(error || errors.length > 0) && <div role="alert">{error}<ul>{errors.map((message, index) => <li key={index}>{message}</li>)}</ul></div>}
    {!review && !error && <p className="loading">Загрузка материалов…</p>}
    {review && !files.length && <p className="empty-state">Добавьте материалы, чтобы сервис предложил темы и авторов докладов.</p>}
    {!!files.length && <div className="material-list"><div className="list-caption"><span>Название и тип</span><span>Состояние</span></div>
      {files.map(file => <details className="material-row" key={file.source.id}>
        <summary><span className="file-type">{file.relative_path.split('.').pop()?.toUpperCase()}</span>
          <span className="file-name">{file.relative_path}<small>{kinds[file.source.kind]}{file.source.duration_ms ? ` · ${Math.ceil(file.source.duration_ms / 1000)} с` : ''}</small></span>
          <span className={file.warning ? 'text-warn' : 'text-ok'}>{file.warning ? 'Нужна проверка' : 'Загружен'}</span></summary>
        <div className="material-details">
          {file.warning && <p className="notice">{file.warning}</p>}
          {['presentation', 'document', 'notes', 'program'].includes(file.source.kind) && <label className="field">Назначение файла
            <select aria-label={`Назначение ${file.relative_path}`} disabled={!!busy} value={file.source.kind} onChange={e => {
              const purpose = e.target.value;
              setBusy('Сохранение назначения…'); setErrors([]);
              void request(`/api/v1/files/${file.source.id}/purpose`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ purpose }) })
                .then(refresh).catch((reason: Error) => setErrors([reason.message])).finally(() => setBusy(''));
            }}>{['presentation', 'document', 'notes', 'program'].map(kind => <option key={kind} value={kind}>{kinds[kind as keyof typeof kinds]}</option>)}</select></label>}
          <a href={`/api/v1/files/${file.source.id}/content`} target="_blank" rel="noreferrer">Открыть оригинал</a>
          {file.title_candidates?.speakers.length ? <p>Авторы в материале — {file.title_candidates.speakers.join(', ')}</p> : null}
        </div>
      </details>)}
    </div>}
    <footer className="step-footer"><p className="muted">{files.length ? `Материалов загружено — ${files.length}. Следующий шаг — выделение докладов.` : 'Начните с загрузки имеющихся материалов.'}</p>
      <button className="primary" disabled={!!busy || !files.length} onClick={() => void prepare()}>Подготовить доклады</button></footer>
  </section>;
}
