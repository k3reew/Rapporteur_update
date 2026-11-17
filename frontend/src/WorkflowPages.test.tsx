import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { parseRoute } from './App';
import { Preparation } from './Preparation';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('старый processing открывает проверку, новые этапы имеют собственные адреса', () => {
  expect(parseRoute('#/c/c/processing')).toEqual({ page: 'conference', id: 'c', tab: 'review' });
  expect(parseRoute('#/c/c/talks')).toEqual({ page: 'conference', id: 'c', tab: 'talks' });
  expect(parseRoute('#/c/c/review')).toEqual({ page: 'conference', id: 'c', tab: 'review' });
});

test('после подтверждения и сбоя запуска предлагает повторить только обработку', async () => {
  let confirmed = false;
  let attempts = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('/processing')) {
      attempts++;
      return attempts === 1 ? Response.json({ message: 'Сервис занят' }, { status: 503 }) : Response.json({});
    }
    if (url.endsWith('/materials')) return Response.json({ files: [], talks: [], sections: [] });
    if (init?.method === 'PUT') confirmed = true;
    return Response.json({ version: confirmed ? 2 : 1, job: confirmed ? 'confirmed' : 'done', error: null,
      talks: [{ talk_id: null, title: 'Доклад', speakers: [], links: [{ file_id: 'f', start_ms: null, end_ms: null, page_start: null, page_end: null }], reason: '' }] });
  }));
  const started = vi.fn();
  render(<Preparation conferenceId="c" onConfirmed={() => {}} onStarted={started} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Подтвердить и обработать' }));
  await screen.findByText('Сервис занят');
  const retry = screen.getByRole('button', { name: 'Запустить обработку' });
  expect(screen.queryByRole('button', { name: 'Подтвердить и обработать' })).toBeNull();
  fireEvent.click(retry);
  await waitFor(() => expect(started).toHaveBeenCalled());
});

test('частичная ошибка загрузки сохраняет успешные файлы и разрешает подготовку', async () => {
  let uploaded = false;
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      if (url.includes('broken')) return Response.json({ message: 'Файл повреждён' }, { status: 422 });
      uploaded = true; return Response.json({}, { status: 201 });
    }
    return Response.json({ files: uploaded ? [{ source: { id: 'f', kind: 'document' }, relative_path: 'ok.txt', warning: null, title_candidates: null }] : [] });
  });
  vi.stubGlobal('fetch', fetch);
  const { Materials } = await import('./Materials');
  const prepared = vi.fn();
  render(<Materials conferenceId="c" onPrepared={prepared} />);
  fireEvent.change(screen.getByLabelText('Отдельные файлы'), { target: { files: [new File(['yes'], 'ok.txt'), new File(['no'], 'broken.txt')] } });
  await screen.findByText('broken.txt — Файл повреждён');
  await screen.findByText('ok.txt');
  fireEvent.click(screen.getByRole('button', { name: 'Подготовить доклады' }));
  await waitFor(() => expect(prepared).toHaveBeenCalled());
});

test('закрытие ручного редактора сохраняет черновик названия', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => Response.json(url.endsWith('/materials')
    ? { files: [], talks: [], sections: [] } : { version: 1, job: 'done', talks: [], error: null })));
  render(<Preparation conferenceId="c" onConfirmed={() => {}} />);
  const disclosure = await screen.findByText('Добавить доклад вручную и настроить привязки');
  fireEvent.click(disclosure);
  const input = await screen.findByLabelText<HTMLInputElement>('Название доклада');
  fireEvent.change(input, { target: { value: 'Мой доклад' } });
  fireEvent.click(disclosure);
  await waitFor(() => expect(disclosure.closest('details')?.open).toBe(false));
  fireEvent.click(disclosure);
  await waitFor(() => expect(screen.getByLabelText<HTMLInputElement>('Название доклада').value).toBe('Мой доклад'));
});

test('интервальные привязки не показываются как отсутствие материалов', async () => {
  const { LegacyTalks } = await import('./LegacyTalks');
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ processing_started: true, sections: [],
    talks: [{ id: 't', title: 'Доклад', section: 'Секция', order: 1, status: 'reviewed' }],
    files: [{ source: { id: 'f', kind: 'audio' }, relative_path: 'audio.wav', binding: { confirmed: true, talk_id: null }, range_talk_ids: ['t'] }] })));
  render(<LegacyTalks conferenceId="c" />);
  await screen.findByLabelText('Название доклада');
  expect(screen.queryByText('Ни один файл не привязан к докладу: сводок не будет.')).toBeNull();
});

test('загрузка папки сохраняет вложенный путь, пропускает реестр и считает дубликат', async () => {
  const { Materials } = await import('./Materials');
  const file = new File(['текст'], 'speech.txt');
  Object.defineProperty(file, 'webkitRelativePath', { value: 'Конференция/Секция/Доклад/speech.txt' });
  const registry = new File(['id: old'], 'conference.yaml');
  Object.defineProperty(registry, 'webkitRelativePath', { value: 'Конференция/conference.yaml' });
  const api = vi.fn(async (_url: string, init?: RequestInit) => Response.json(init?.method === 'POST' ? {} : { files: [] }));
  vi.stubGlobal('fetch', api);
  render(<Materials conferenceId="c" onPrepared={() => {}} />);
  fireEvent.change(screen.getByLabelText('Папка конференции'), { target: { files: [file, registry] } });
  await screen.findByText('Загружено 0, уже было в конференции 1.');
  const uploads = api.mock.calls.filter(([, init]) => init?.method === 'POST');
  expect(uploads).toHaveLength(1);
  expect(new URL('http://local'+uploads[0][0]).searchParams.get('path')).toBe('Секция/Доклад/speech.txt');
});

test('смена назначения файла отправляет новое значение и перечитывает список', async () => {
  const { Materials } = await import('./Materials');
  let purpose = 'presentation';
  const api = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('/purpose')) purpose = JSON.parse(init?.body as string).purpose;
    return Response.json({ files: [{ source: { id: 'f', kind: purpose }, relative_path: 'slides.pdf', title_candidates: null, warning: null }] });
  });
  vi.stubGlobal('fetch', api);
  render(<Materials conferenceId="c" onPrepared={() => {}} />);
  const select = await screen.findByLabelText<HTMLSelectElement>('Назначение slides.pdf');
  fireEvent.change(select, { target: { value: 'program' } });
  await waitFor(() => expect(select.value).toBe('program'));
  expect(api).toHaveBeenCalledWith('/api/v1/files/f/purpose', expect.objectContaining({ method: 'PUT', body: JSON.stringify({ purpose: 'program' }) }));
});
