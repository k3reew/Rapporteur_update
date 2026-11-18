import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

import { Processing } from './Processing';
import { Preparation } from './Preparation';
import { SummaryReview } from './SummaryReview';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const ready = { id: 'one', title: 'Первый', section: 'Секция', order: 1, status: 'processed', job: 'done', step: null,
  error: null, summary_number: 1, summary_status: 'ready', bulk_reviewable: true };
const summary = { talk_title: 'Первый', talk_status: 'processed', pack: { fragments: [] },
  summary: { version: 1, status: 'ready', profile: 'local', human_edit: false, sentences: [{ text: 'Факт.', claim_ids: [] }], conclusions: [], claims: [] } };
function mockReview() {
  const api = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('/progress')) return Response.json({ talks: [ready, { ...ready, id: 'two', order: 2 }] });
    if (url.endsWith('/recommendations')) return Response.json({ version: 0, items: [], reviewed: false, stale: false });
    if (url.endsWith('/departments')) return Response.json([]);
    if (url.endsWith('/summary/review')) return Response.json({ ...summary, talk_status: 'reviewed', summary: { ...summary.summary, version: 2 } });
    if (url.endsWith('/summary')) {
      if (init?.method === 'PUT') return Response.json({ message: 'Конфликт версии' }, { status: 409 });
      return Response.json(summary);
    }
    return Response.json({ title: 'Первый', speakers: [], revision: 0 });
  });
  vi.stubGlobal('fetch', api);
  return api;
}

test('подтверждает открытый доклад и переходит к следующему', async () => {
  const api = mockReview(); const next = vi.fn();
  render(<SummaryReview talkId="one" conferenceId="c" onNavigate={next} onClose={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Подтвердить и следующий' }));
  await waitFor(() => expect(next).toHaveBeenCalledWith('two'));
  expect(api).toHaveBeenCalledWith('/api/v1/talks/one/summary/review', expect.objectContaining({ body: JSON.stringify({ version: 1 }) }));
});

test('не теряет правку при конфликте и не переходит дальше', async () => {
  mockReview(); const next = vi.fn();
  render(<SummaryReview talkId="one" conferenceId="c" onNavigate={next} onClose={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Править' }));
  fireEvent.change(screen.getByLabelText('Предложение 1'), { target: { value: 'Измененный факт.' } });
  await screen.findByText('Доклад 1 из 2');
  fireEvent.click(screen.getByRole('button', { name: /^Следующий$/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Сохранить и перейти' }));
  await screen.findByText('Конфликт версии');
  expect(next).not.toHaveBeenCalled();
  expect(screen.getByLabelText<HTMLTextAreaElement>('Предложение 1').value).toBe('Измененный факт.');
});

test('массовая проверка отправляет только разрешенные сервером версии', async () => {
  const api = vi.fn(async (url: string) => url.endsWith('/progress')
    ? Response.json({ processing_started: true, files: 2, talks: [ready, { ...ready, id: 'visual', bulk_reviewable: false }] })
    : Response.json({ confirmed: ['one'], skipped: [] }));
  vi.stubGlobal('fetch', api);
  render(<Processing conferenceId="c" onOpen={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Подтвердить все готовые (1)' }));
  await screen.findByText('Подтверждено: 1.');
  expect(api).toHaveBeenCalledWith('/api/v1/conferences/c/summaries/review', expect.objectContaining({ body: JSON.stringify({ items: [{ talk_id: 'one', version: 1 }] }) }));
});

test('редактирует временные границы предложения и отправляет его версию', async () => {
  const proposal = { version: 3, job: 'done', error: null, talks: [{ talk_id: null, title: 'Доклад', speakers: [], reason: 'Проверить',
    links: [{ file_id: 'audio', start_ms: 0, end_ms: 10000, page_start: null, page_end: null }] }] };
  const api = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('/materials')) return Response.json({ files: [{ source: { id: 'audio', kind: 'audio', duration_ms: 10000 }, relative_path: 'запись.wav' }] });
    return Response.json(init?.method === 'PUT' ? { ...proposal, job: 'confirmed' } : proposal);
  });
  vi.stubGlobal('fetch', api); const confirmed = vi.fn();
  render(<Preparation conferenceId="c" onConfirmed={confirmed} />);
  fireEvent.change(await screen.findByLabelText('Начало, с'), { target: { value: '1.5' } });
  fireEvent.click(screen.getByRole('button', { name: 'Подтвердить и обработать' }));
  await waitFor(() => expect(confirmed).toHaveBeenCalled());
  const sent = api.mock.calls.find(([, init]) => init?.method === 'PUT');
  expect(JSON.parse(sent?.[1]?.body as string).talks[0].links[0].start_ms).toBe(1500);
  expect(JSON.parse(sent?.[1]?.body as string).version).toBe(3);
});
