from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify
import shared

views_bp = Blueprint('views', __name__)

@views_bp.route('/')
def index():
    """渲染還書介面主頁"""
    if shared.is_box_full():
        return render_template('return_book.html', service_suspended=True)
    return render_template('return_book.html', service_suspended=False)

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
    return render_template('admin.html', config=current_config)