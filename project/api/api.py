import os
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import mysql.connector.pooling
import logging

logging.basicConfig(
    level=logging.DEBUG,
    filename='api.log',
    format='%(asctime)s - %(levelname)s - %(message)s'
)

app = Flask(__name__)
CORS(app)

# Конфигурация
app.config['UPLOAD_FOLDER'] = os.path.abspath('C:\\send_to_ptint\\send-to-print\\project\\api\\uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Пул соединений MySQL
db_pool = mysql.connector.pooling.MySQLConnectionPool(
    pool_name="api_pool",
    pool_size=15,
    user='root',
    password='3465',
    host='localhost',
    database='send_to_print'
)


def get_db_connection():
    return db_pool.get_connection()

def schedule_file_deletion(file_path, delay):
    def delete():
        time.sleep(delay)
        if os.path.exists(file_path):
            os.remove(file_path)
    threading.Thread(target=delete).start()


@app.route('/api/orders', methods=['POST'])
def create_order():
    conn = None
    cursor = None
    file = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Парсинг данных
        data = request.form
        file = request.files['file']

        # Валидация
        required_fields = ['ID_shop', 'price', 'con_code', 'color', 'user_id', 'pages']
        for field in required_fields:
            if field not in data:
                raise ValueError(f"Отсутствует поле: {field}")

        # Сохранение файла
        ext = os.path.splitext(file.filename)[1]
        filename = f"order_{uuid.uuid4()}{ext}"
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)

        # Запись в БД
        cursor.execute("""
            INSERT INTO `order` 
            (ID_shop, price, note, con_code, color, status, user_id, pages, file_extension, file_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            data['ID_shop'],
            data['price'],
            data.get('note', ''),
            data['con_code'],
            data['color'],
            'получен',
            data['user_id'],
            data['pages'],
            ext[1:],
            filename
        ))

        conn.commit()
        order_id = cursor.lastrowid

        # Запуск таймера удаления файла (1 час)
        threading.Thread(target=schedule_file_deletion, args=(file_path, 3600)).start()

        return jsonify({
            "status": "success",
            "order_id": order_id,
            "file_path": filename
        }), 201

    except Exception as e:
        if conn: conn.rollback()
        logging.error(f"API Error: {traceback.format_exc()}")
        if file and 'file_path' in locals():
            if os.path.exists(file_path):
                os.remove(file_path)
        return jsonify({"status": "error", "message": str(e)}), 500

    finally:
        if cursor: cursor.close()
        if conn: conn.close()

@app.route('/api/orders', methods=['GET'])
def get_orders():
    try:
        status = request.args.get('status', 'received')
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM `order` WHERE status = %s", ('получен',))
        orders = cursor.fetchall()
        return jsonify(orders)
    except Exception as e:
        return jsonify({"status": "error"}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/files/<filename>', methods=['GET'])
def get_file(filename):
    try:
        # Проверка существования файла
        if not os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], filename)):
            return jsonify({"status": "error", "message": "File not found"}), 404

        # Отправка с заголовком Content-Length
        response = send_from_directory(
            app.config['UPLOAD_FOLDER'],
            filename,
            as_attachment=False
        )
        response.headers["Content-Length"] = os.path.getsize(
            os.path.join(app.config['UPLOAD_FOLDER'], filename)
        )
        return response
    except Exception as e:
        logging.error(f"API File Error: {str(e)}")
        return jsonify({"status": "error"}), 500


@app.route('/api/orders/<int:order_id>', methods=['PUT'])
def update_order(order_id):
    try:
        data = request.json

        # Разрешенные поля для обновления
        allowed_fields = {'status', 'file_path'}
        update_data = {k: v for k, v in data.items() if k in allowed_fields}

        conn = get_db_connection()
        cursor = conn.cursor()

        if update_data:
            query = "UPDATE `order` SET "
            query += ", ".join([f"{key} = %s" for key in update_data.keys()])
            query += " WHERE ID = %s"

            values = list(update_data.values()) + [order_id]
            cursor.execute(query, values)
            conn.commit()

        return jsonify({"status": "success"})

    except Exception as e:
        app.logger.error(f"Ошибка обновления заказа: {str(e)}")
        return jsonify({"status": "error"}), 500

    finally:
        cursor.close()
        conn.close()

@app.route('/api/orders/<int:order_id>', methods=['DELETE'])
def delete_order(order_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM `order` WHERE ID = %s", (order_id,))
        order = cursor.fetchone()

        if not order:
            return jsonify({"status": "error", "message": "Order not found"}), 404

        # Удаление из UPLOAD_FOLDER
        uploads_path = os.path.join(app.config['UPLOAD_FOLDER'], order['file_path'])
        if os.path.exists(uploads_path):
            os.remove(uploads_path)

        # Удаление из DOWNLOAD_DIR (если существует)
        download_path = os.path.join("D:\\projects_py\\projectsWithGit\\send-to-print\\project\\api\\uploads", order['file_path'])
        if os.path.exists(download_path):
            os.remove(download_path)

        cursor.execute("DELETE FROM `order` WHERE ID = %s", (order_id,))
        conn.commit()
        return jsonify({"status": "success"})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)