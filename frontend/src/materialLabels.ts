import type { Material } from './materialTypes';
export const kinds: Record<Material['source']['kind'], string> = {
  document: 'Текст выступления',
  image: 'Фото слайда',
  presentation: 'Презентация',
  audio: 'Аудио',
  video: 'Видео',
  notes: 'Заметки',
  program: 'Программа',
};
