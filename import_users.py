import pymongo
import os
from dotenv import load_dotenv

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = "squad_clan_db"

def run_import():
    file_path = "users_data.txt"
    
    if not os.path.exists(file_path):
        print(f"Ошибка: Файл '{file_path}' не найден!")
        print("Создай этот файл и вставь туда скопированные строки из таблицы.")
        return

    print("Подключение к базе данных...")
    try:
        client = pymongo.MongoClient(MONGO_URI)
        db = client[DB_NAME]
        collection = db["users"]
    except Exception as e:
        print(f"Ошибка подключения к БД: {e}")
        return

    operations = []
    
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    for line_num, line in enumerate(lines, 1):
        if not line.strip():
            continue
            
        parts = line.split("\t")
        
        if len(parts) >= 4:
            nickname = parts[0].strip()
            steam_id = parts[1].strip()
            discord_name = parts[2].strip() 
            
            try:
                discord_id = int(parts[3].strip()) 
            except ValueError:
                print(f"Ошибка в строке {line_num} (неверный Discord ID): {line.strip()}")
                continue

            op = pymongo.UpdateOne(
                {"discord_id": discord_id},
                {"$set": {
                    "discord_id": discord_id,
                    "steam_id": steam_id,
                    "discord_name": discord_name,
                    "squad_nickname": nickname
                }},
                upsert=True
            )
            operations.append(op)
        else:
            print(f"Пропущена строка {line_num}: недостаточно колонок (нужно 4).")

    if operations:
        print(f"Готово к импорту: {len(operations)} записей. Выполняю...")
        result = collection.bulk_write(operations)
        print(f"Успех! Добавлено новых: {result.upserted_count}. Обновлено старых: {result.modified_count}.")
    else:
        print("Нет валидных данных для импорта. Проверь файл users_data.txt.")

if __name__ == "__main__":
    run_import()