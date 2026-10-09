"""Offline shadow learning from the recorded paper journal. No market requests."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import shortlab
import shortlearn
import shorts


def main():
    parser = argparse.ArgumentParser(description='Обучение теневой модели виртуальных шортов')
    parser.add_argument('--db', default=shorts.DB_PATH, help='Существующий журнал наблюдений')
    parser.add_argument('--journal', default=shortlab.DB_PATH, help='Отдельный журнал исследования')
    parser.add_argument('--model', default=shortlab.MODEL_PATH, help='Файл проверенной теневой модели')
    args = parser.parse_args()
    result = shortlab.train(args.db, args.journal, args.model)
    print(json.dumps({'trained': result['trained'], 'ready': result['ready'],
                      'reason': result['reason'], 'evaluation': result['evaluation']},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
