import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

import { Processing } from './Processing';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const talk = {
  id: 't1', section_id: 's', section: 'Секция', order: 1, status: 'processed', job: 'done', step: null,
  error: null, summary_number: null, summary_status: null, bulk_reviewable: false,
};
const screening = (state: string, ids: string[]) => ({
  version: 1, state, eligible: state === 'relevant', stale: false, manual: false, error: null, department_ids: ids,
  items: [{ department_id: 'd', department_name: 'Отдел', status: state, reason: 'Основание', evidence: [], paragraphs: [] }],
});

test('доклад без интереса отделов получает только карточку, оператор может это решить сам', async () => {
  const puts: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'PUT') {
      puts.push([url, JSON.parse(String(init.body))]);
      return Response.json({});
    }
    return Response.json({
      processing_started: true,
      files: 2,
      talks: [
        { ...talk, title: 'Кэширование', card_only: true, screening: screening('not_relevant', []) },
        { ...talk, id: 't2', order: 2, title: 'Сжатие', card_only: false, summary_number: 1, screening: screening('relevant', ['d']) },
      ],
    });
  }));
  render(<Processing conferenceId="c" onOpen={() => {}} />);

  expect(await screen.findByText('только карточка')).toBeDefined();
  expect(screen.getByText(/только карточка — у 1/)).toBeDefined();
  expect(screen.getByText('Интерес не найден — только карточка')).toBeDefined();
  const buttons = screen.getAllByRole<HTMLButtonElement>('button', { name: 'Открыть сводку' });
  expect(buttons.map(button => button.disabled)).toEqual([true, false]);

  fireEvent.click(screen.getByText('Есть интерес отделов'));
  // У каждого доклада своя кнопка, вторая — у «Сжатия».
  fireEvent.click(screen.getAllByRole('button', { name: 'Только карточка' })[1]);
  await vi.waitFor(() => expect(puts).toEqual([['/api/v1/talks/t2/screening', { version: 1, department_ids: [] }]]));
});
