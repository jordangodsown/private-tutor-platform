import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()
from flask import Flask, render_template, redirect, url_for, flash, request, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_mail import Mail, Message as MailMessage
from models import db, User, TutorProfile, StudentProfile, Booking, Review, Message, Notification, IDVerification
from functools import wraps
from threading import Thread

from datetime import datetime
import cloudinary
import cloudinary.uploader
import cloudinary.api

app = Flask(__name__)
csrf = CSRFProtect(app)
# Secret key for session management
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super-secret-key-for-dev')
BaseDir = os.path.abspath(os.path.dirname(__file__))

# Database configuration
if os.environ.get('DATABASE_URL'):
    database_url = os.environ.get('DATABASE_URL')
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
else:
    # Local development only - SQLite fallback
    database_url = 'sqlite:///' + os.path.join(BaseDir, 'private_tutor.db')

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}

# Mail config (Using env vars, or placeholders to be filled)
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', 'your_email@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', 'your_app_password')
app.config['MAIL_DEFAULT_SENDER'] = app.config['MAIL_USERNAME']

# Configure Cloudinary
cloudinary_cloud_name = os.environ.get('CLOUDINARY_CLOUD_NAME')
cloudinary_api_key = os.environ.get('CLOUDINARY_API_KEY')
cloudinary_api_secret = os.environ.get('CLOUDINARY_API_SECRET')
cloudinary.config(
    cloud_name=cloudinary_cloud_name,
    api_key=cloudinary_api_key,
    api_secret=cloudinary_api_secret
)
if not all([cloudinary_cloud_name, cloudinary_api_key, cloudinary_api_secret]):
    print('Warning: Cloudinary credentials are not fully configured. ID and profile photo uploads may fail.')

# Upload config
app.config['UPLOAD_FOLDER'] = os.path.join(BaseDir, 'static', 'uploads', 'profiles')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 # 16 MB limit

# Initialize extensions
db.init_app(app)
mail = Mail(app)

# Proxy Fix for SSL on Render
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Configure OAuth client
from authlib.integrations.flask_client import OAuth
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.environ.get('GOOGLE_CLIENT_ID'),
    client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

def send_async_email(app, msg):
    with app.app_context():
        try:
            mail.send(msg)
        except Exception as e:
            print(f"Background email failed (expected on Render free tier without setup): {e}")

login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Initialize Database Context
with app.app_context():
    try:
        db.create_all()
        # Auto-migration: Ensure new columns exist on Render (Postgres)
        if 'postgresql' in database_url:
            from sqlalchemy import text
            try:
                db.session.execute(text("ALTER TABLE tutor_profile ADD COLUMN IF NOT EXISTS id_verified BOOLEAN DEFAULT FALSE;"))
                db.session.execute(text("ALTER TABLE tutor_profile ADD COLUMN IF NOT EXISTS id_document VARCHAR(255);"))
                db.session.commit()
                print("Postgres database migrations applied successfully.")
            except Exception as migration_error:
                db.session.rollback()
                print(f"Postgres auto-migration warning: {migration_error}")
    except Exception as e:
        print(f"Warning: Database initialization failed on startup (app will still boot): {e}")

@app.context_processor
def inject_notifications():
    if current_user.is_authenticated:
        unread_notifications = Notification.query.filter_by(user_id=current_user.id, is_read=False).order_by(Notification.created_at.desc()).all()
        return dict(unread_notifications=unread_notifications, unread_count=len(unread_notifications))
    return dict(unread_notifications=[], unread_count=0)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        role = request.form.get('role')

        existing_admin = User.query.filter_by(is_admin=True).first()
        is_admin = False
        if existing_admin is None:
            is_admin = True
            flash('This is the first registered account, so it has been created as the admin.', 'info')

        user_exists = User.query.filter_by(email=email).first()
        if user_exists:
            flash('Email already in use.', 'warning')
            return redirect(url_for('register'))

        hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
        new_user = User(username=username, email=email, password=hashed_pw, role=role, is_admin=is_admin)
        db.session.add(new_user)
        db.session.commit()

        if role == 'tutor':
            new_profile = TutorProfile(
                user_id=new_user.id,
                bio='',
                subjects='',
                hourly_rate=0.0
            )
            db.session.add(new_profile)
            db.session.commit()
        elif role == 'student':
            new_profile = StudentProfile(user_id=new_user.id)
            db.session.add(new_profile)
            db.session.commit()

        flash('Account created successfully! You can now login.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            flash('Logged in successfully.', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password.', 'danger')

    return render_template('login.html')

@app.route('/login/google')
def login_google():
    role = request.args.get('role', 'student')
    session['oauth_role'] = role
    redirect_uri = url_for('google_authorize', _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route('/login/google/authorize')
def google_authorize():
    try:
        token = google.authorize_access_token()
    except Exception as e:
        print(f"Google OAuth authorization failed: {e}")
        flash('Google authorization failed.', 'danger')
        return redirect(url_for('login'))
        
    user_info = token.get('userinfo')
    if not user_info:
        flash('Failed to retrieve user info from Google.', 'danger')
        return redirect(url_for('login'))
    
    email = user_info.get('email')
    name = user_info.get('name', email.split('@')[0])
    
    # Check if user exists
    user = User.query.filter_by(email=email).first()
    if not user:
        # Create new user
        role = session.pop('oauth_role', 'student')
        
        # Check if base username is already taken
        import secrets
        base_username = name
        username = base_username
        counter = 1
        while User.query.filter_by(username=username).first():
            username = f"{base_username}{counter}"
            counter += 1
            
        # Generate a random password since password is non-nullable
        random_pw = secrets.token_hex(16)
        hashed_pw = generate_password_hash(random_pw, method='pbkdf2:pbkdf2:sha256')
        
        existing_admin = User.query.filter_by(is_admin=True).first()
        is_admin = existing_admin is None
        if is_admin:
            flash('This is the first account created via Google login, so it has been created as the admin.', 'info')

        user = User(username=username, email=email, password=hashed_pw, role=role, is_admin=is_admin)
        db.session.add(user)
        db.session.commit()
        
        # Create corresponding profile
        if role == 'tutor':
            profile = TutorProfile(user_id=user.id)
        else:
            profile = StudentProfile(user_id=user.id)
        db.session.add(profile)
        db.session.commit()
        
        flash('Account created successfully via Google!', 'success')
    else:
        # If the user already exists, consume oauth_role from session just to clean it up
        session.pop('oauth_role', None)
        
    login_user(user)
    flash('Logged in successfully via Google.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))

@app.route('/tutors')
def tutors():
    subject_query = request.args.get('subject', '')
    if subject_query:
        # Search by subject
        tutors = TutorProfile.query.filter(TutorProfile.subjects.ilike(f'%{subject_query}%')).all()
    else:
        tutors = TutorProfile.query.all()
    return render_template('tutors.html', tutors=tutors, query=subject_query, today=datetime.utcnow().date().isoformat())

@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.role == 'student':
        bookings = Booking.query.filter_by(student_id=current_user.id).order_by(Booking.date.asc()).all()
    else:
        bookings = Booking.query.filter_by(tutor_id=current_user.tutor_profile.id).order_by(Booking.date.asc()).all()

    messages = Message.query.filter((Message.receiver_id == current_user.id) | (Message.sender_id == current_user.id)).order_by(Message.timestamp.desc()).all()
    return render_template('dashboard.html', bookings=bookings, messages=messages)

@app.route('/book_tutor/<int:tutor_id>', methods=['POST'])
@login_required
def book_tutor(tutor_id):
    if current_user.role != 'student':
        flash('Only students can book tutors.', 'warning')
        return redirect(url_for('tutors'))
    
    # Check if tutor exists
    tutor_profile = TutorProfile.query.get(tutor_id)
    if not tutor_profile:
        flash('Tutor not found. Please try again.', 'danger')
        return redirect(url_for('tutors'))
        
    subject = request.form.get('subject')
    date_str = request.form.get('date')
    time_str = request.form.get('time')
    
    try:
        booking_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        if time_str.count(':') == 2:
            booking_time = datetime.strptime(time_str, '%H:%M:%S').time()
        else:
            booking_time = datetime.strptime(time_str, '%H:%M').time()
        current_datetime = datetime.utcnow()
        if booking_date < current_datetime.date() or (booking_date == current_datetime.date() and booking_time <= current_datetime.time()):
            flash('The date/time you chose has already passed. Please select a future appointment.', 'warning')
            return redirect(url_for('tutors'))
    except Exception as e:
        print(f"Error parsing date/time: {e} (date: {date_str}, time: {time_str})")
        flash('Invalid date or time format. Please try again.', 'danger')
        return redirect(url_for('tutors'))
    
    try:
        new_booking = Booking(
            student_id=current_user.id,
            tutor_id=tutor_id,
            subject=subject,
            date=booking_date,
            time=booking_time
        )
        db.session.add(new_booking)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Database error during booking: {e}")
        flash('An error occurred saving your booking. Please try again later.', 'danger')
        return redirect(url_for('tutors'))
    
    # Create notification for tutor
    tutor_profile = TutorProfile.query.get(tutor_id)
    if tutor_profile:
        try:
            notification = Notification(
                user_id=tutor_profile.user_id,
                message=f"New tutoring request from {current_user.username} for {subject}."
            )
            db.session.add(notification)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"Database error creating notification: {e}")
    
        # Send email to tutor
        if tutor_profile.user and tutor_profile.user.email:
            msg = MailMessage('New Tutoring Session Request', recipients=[tutor_profile.user.email])
            msg.body = f"Hello {tutor_profile.user.username},\n\nYou have a new tutoring session request from {current_user.username} for {subject} on {booking_date} at {booking_time}.\nPlease log in to your dashboard to confirm or decline."
            # Send email in background thread so it doesn't freeze the app
            Thread(target=send_async_email, args=(app, msg)).start()
            flash('Booking request sent successfully (email notification queued)!', 'success')
        else:
            flash('Booking request sent successfully!', 'success')
    else:
        flash('Tutor not found. Please try again.', 'danger')
        return redirect(url_for('tutors'))
        
    return redirect(url_for('dashboard'))

@app.route('/update_profile', methods=['POST'])
@login_required
def update_profile():
    bio = request.form.get('bio')
    profile_photo = request.files.get('profile_photo')
    
    if current_user.is_admin:
        profile = current_user.tutor_profile or current_user.student_profile
        if not profile:
            profile = StudentProfile(user_id=current_user.id)
            db.session.add(profile)
        profile.bio = bio
    elif current_user.role == 'tutor':
        subjects = request.form.get('subjects')
        hourly_rate = request.form.get('hourly_rate')
        
        profile = current_user.tutor_profile
        if not profile:
            profile = TutorProfile(user_id=current_user.id)
            db.session.add(profile)
        profile.subjects = subjects
        profile.hourly_rate = float(hourly_rate) if hourly_rate else 0.0
        profile.bio = bio
    elif current_user.role == 'student':
        grade_level = request.form.get('grade_level')
        
        profile = current_user.student_profile
        if not profile:
            profile = StudentProfile(user_id=current_user.id)
            db.session.add(profile)
        profile.grade_level = grade_level
        profile.bio = bio
    else:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('dashboard'))
    
    if profile_photo and profile_photo.filename != '':
        try:
            upload_result = cloudinary.uploader.upload(profile_photo)
            profile.profile_photo = upload_result['secure_url']
        except Exception as e:
            print(f"Cloudinary upload failed: {e}")
            flash('Error uploading photo to cloud storage.', 'warning')
    
    db.session.commit()
    flash('Profile updated successfully!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/update_booking/<int:booking_id>', methods=['POST'])
@login_required
def update_booking_status(booking_id):
    if current_user.role != 'tutor':
        flash('Unauthorized.', 'danger')
        return redirect(url_for('dashboard'))
        
    status = request.form.get('status')
    booking = Booking.query.get_or_404(booking_id)
    
    if booking.tutor_id != current_user.tutor_profile.id:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('dashboard'))
        
    booking.status = status
    db.session.commit()
    
    student_user = booking.student
    if student_user:
        notification = Notification(
            user_id=student_user.id,
            message=f"Your {booking.subject} session was {status} by your tutor."
        )
        db.session.add(notification)
        db.session.commit()
    
    if status == 'confirmed':
        if student_user and student_user.email:
            msg = MailMessage('Tutoring Session Confirmed', recipients=[student_user.email])
            msg.body = f"Hello {student_user.username},\n\nYour tutoring session for {booking.subject} on {booking.date} at {booking.time} has been confirmed by your tutor.\n\nSee you then!"
            Thread(target=send_async_email, args=(app, msg)).start()
    
    flash('Booking status updated.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/notifications/read/<int:notif_id>', methods=['POST'])
@login_required
def read_notification(notif_id):
    notif = Notification.query.get_or_404(notif_id)
    if notif.user_id == current_user.id:
        notif.is_read = True
        db.session.commit()
    return redirect(request.referrer or url_for('dashboard'))

@app.route('/send_message/<int:receiver_id>', methods=['POST'])
@login_required
def send_message(receiver_id):
    content = request.form.get('content')
    if content:
        msg = Message(sender_id=current_user.id, receiver_id=receiver_id, content=content)
        db.session.add(msg)
        notification = Notification(
            user_id=receiver_id,
            message=f"New message from {current_user.username}"
        )
        db.session.add(notification)
        db.session.commit()
        flash('Message sent!', 'success')
    return redirect(request.referrer or url_for('dashboard'))

# Admin Authentication Decorator
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('You must be an admin to access this page.', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# ADMIN PANEL ROUTES
@app.route('/admin')
@admin_required
def admin_panel():
    users = User.query.order_by(User.date_joined.desc()).all()
    pending_verifications = IDVerification.query.filter_by(status='pending').all()
    tutor_profiles = TutorProfile.query.order_by(TutorProfile.id.desc()).all()
    total_users = User.query.count()
    total_tutors = User.query.filter_by(role='tutor').count()
    total_students = User.query.filter_by(role='student').count()
    total_bookings = Booking.query.count()
    total_reviews = Review.query.count()
    booking_counts = {
        'pending': Booking.query.filter_by(status='pending').count(),
        'confirmed': Booking.query.filter_by(status='confirmed').count(),
        'completed': Booking.query.filter_by(status='completed').count(),
        'canceled': Booking.query.filter_by(status='canceled').count(),
    }
    verification_counts = {
        'pending': IDVerification.query.filter_by(status='pending').count(),
        'approved': IDVerification.query.filter_by(status='approved').count(),
        'rejected': IDVerification.query.filter_by(status='rejected').count(),
    }
    recent_bookings = Booking.query.order_by(Booking.created_at.desc()).limit(8).all()
    recent_verifications = IDVerification.query.order_by(IDVerification.submission_date.desc()).limit(8).all()
    return render_template(
        'admin.html',
        users=users,
        pending_verifications=pending_verifications,
        tutor_profiles=tutor_profiles,
        total_users=total_users,
        total_tutors=total_tutors,
        total_students=total_students,
        total_bookings=total_bookings,
        total_reviews=total_reviews,
        booking_counts=booking_counts,
        verification_counts=verification_counts,
        recent_bookings=recent_bookings,
        recent_verifications=recent_verifications
    )

@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@admin_required
def admin_delete_user(user_id):
    if user_id == current_user.id:
        flash('You cannot delete your own account.', 'danger')
        return redirect(url_for('admin_panel'))
    
    user = User.query.get_or_404(user_id)
    username = user.username
    db.session.delete(user)
    db.session.commit()
    flash(f'User {username} deleted successfully.', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/promote_admin/<int:user_id>', methods=['POST'])
@admin_required
def admin_promote_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.is_admin:
        flash(f'User {user.username} is already an admin.', 'info')
        return redirect(url_for('admin_panel'))
    user.is_admin = True
    db.session.commit()
    flash(f'User {user.username} promoted to admin.', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/unverified')
@admin_required
def admin_unverified():
    pending = IDVerification.query.filter_by(status='pending').all()
    return render_template('admin_unverified.html', pending_verifications=pending)

@app.route('/admin/demote_admin/<int:user_id>', methods=['POST'])
@admin_required
def admin_demote_user(user_id):
    if user_id == current_user.id:
        flash('You cannot demote yourself.', 'danger')
        return redirect(url_for('admin_panel'))
    
    user = User.query.get_or_404(user_id)
    user.is_admin = False
    db.session.commit()
    flash(f'User {user.username} demoted from admin.', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/tutor/upload_id', methods=['GET', 'POST'])
@login_required
def tutor_upload_id():
    if current_user.role != 'tutor':
        flash('Only tutors can upload ID documents.', 'warning')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        id_document = request.files.get('id_document')
        
        if not id_document or id_document.filename == '':
            flash('Please select a file to upload.', 'danger')
            return redirect(url_for('tutor_upload_id'))
        
        # Allowed file extensions
        ALLOWED_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png', 'gif'}
        if '.' not in id_document.filename or id_document.filename.rsplit('.', 1)[1].lower() not in ALLOWED_EXTENSIONS:
            flash('Invalid file type. Please upload PDF, JPG, PNG, or GIF.', 'danger')
            return redirect(url_for('tutor_upload_id'))
        
        try:
            upload_result = cloudinary.uploader.upload(id_document)
            unique_filename = upload_result.get('secure_url')
            
            if not unique_filename:
                raise RuntimeError('Cloudinary upload did not return a secure URL.')

            # Update or create ID verification record
            tutor_profile = current_user.tutor_profile
            if not tutor_profile:
                tutor_profile = TutorProfile(user_id=current_user.id)
                db.session.add(tutor_profile)
                db.session.commit()

            tutor_profile.id_document = unique_filename
            db.session.commit()
            
            # Create IDVerification record
            existing_verification = IDVerification.query.filter_by(tutor_profile_id=tutor_profile.id).first()
            if existing_verification:
                existing_verification.id_document = unique_filename
                existing_verification.submission_date = datetime.utcnow()
                existing_verification.status = 'pending'
            else:
                verification = IDVerification(
                    tutor_profile_id=tutor_profile.id,
                    id_document=unique_filename,
                    status='pending'
                )
                db.session.add(verification)
            db.session.commit()
            
            flash('ID document uploaded successfully! Awaiting admin verification.', 'success')
        except Exception as e:
            print(f"Error uploading ID: {e}")
            flash(f'An error occurred uploading your ID: {e}', 'danger')
    
    tutor_profile = current_user.tutor_profile
    id_verification = IDVerification.query.filter_by(tutor_profile_id=tutor_profile.id).first() if tutor_profile else None
    return render_template('tutor_upload_id.html', id_verification=id_verification)

@app.route('/admin/verify_tutor/<int:verification_id>', methods=['POST'])
@admin_required
def admin_verify_tutor(verification_id):
    verification = IDVerification.query.get_or_404(verification_id)
    action = request.form.get('action')  # 'approve' or 'reject'
    admin_notes = request.form.get('admin_notes', '')
    
    if action == 'approve':
        verification.status = 'approved'
        verification.verified_by_admin = current_user.id
        verification.admin_notes = admin_notes
        verification.tutor_profile.id_verified = True
        db.session.commit()
        
        # Send notification to tutor
        tutor_user = verification.tutor_profile.user
        notification = Notification(
            user_id=tutor_user.id,
            message=f"Your ID has been verified by admin! You are now a trusted tutor."
        )
        db.session.add(notification)
        db.session.commit()
        
        flash(f'Tutor {tutor_user.username} verified successfully.', 'success')
    elif action == 'reject':
        verification.status = 'rejected'
        verification.verified_by_admin = current_user.id
        verification.admin_notes = admin_notes
        db.session.commit()
        
        # Send notification to tutor
        tutor_user = verification.tutor_profile.user
        notification = Notification(
            user_id=tutor_user.id,
            message=f"Your ID verification was rejected. Reason: {admin_notes}"
        )
        db.session.add(notification)
        db.session.commit()
        
        flash(f'Tutor {tutor_user.username} verification rejected.', 'success')
    
    return redirect(url_for('admin_panel'))

if __name__ == '__main__':
    app.run(debug=True)
