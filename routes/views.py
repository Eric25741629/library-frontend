from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify
import shared

views_bp = Blueprint('views', __name__)

@views_bp.route('/')
def index():
    """渲染還書介面主頁"""
    # 前端圖片設定（可透過後台上傳變更），從 shared.cfg 讀取
    front_images = (shared.cfg or {}).get('front_images', {}) if hasattr(shared, 'cfg') else {}
    scan_guide = front_images.get('scan_guide')
    place_book_guide = front_images.get('place_book_guide')

    ctx = {
        'service_suspended': shared.is_box_full(),
        'scan_guide_image': scan_guide,
        'place_book_guide_image': place_book_guide,
        # 是否啟用書籍檢測，提供前台顯示狀態用
        'book_check_enabled': getattr(shared, 'BOOK_CHECK_ENABLED', True),
    }
    return render_template('return_book.html', **ctx)

@views_bp.route('/login', methods=['GET', 'POST'])
def login():
    """管理員登入"""
    if request.method == 'POST':
        data = request.json
        password = data.get('password')
        if password == shared.ADMIN_PASSWORD:
            session.permanent = True
            session['logged_in'] = True
            return jsonify({"success": True})
        return jsonify({"success": False, "message": "密碼錯誤"})
    return render_template('login.html', barcode_login_enabled=shared.BARCODE_LOGIN_ENABLED)

@views_bp.route('/logout')
def logout():
    """管理員登出"""
    session.pop('logged_in', None)
    return redirect(url_for('views.index'))

@views_bp.route('/admin')
def admin():
    """渲染管理員後台"""
    if shared.REQUIRE_ADMIN_LOGIN and not session.get('logged_in'):
        return redirect(url_for('views.login'))
    # Pass current config to template for pre-filling forms
    current_config = {
        'max_return_limit': shared.MAX_RETURN_LIMIT
    }
    return render_template('admin.html', config=current_config, app_version=getattr(shared, 'APP_VERSION', 'unknown'))