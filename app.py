import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()
from flask import Flask, render_template, redirect, url_for, flash, request
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_mail import Mail, Message as MailMessage
from models import db, User, TutorProfile, StudentProfile, Booking, Review, Message, Notification

from datetime import datetime

app = Flask(__name__)
# Secret key for session management
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super-secret-key-for-dev')
BaseDir = os.path.abspath(os.path.dirname(__file__))
database_url = os.environ.get('DATABASE_URL', 'sqlite:///' + os.path.join(BaseDir, 'private_tutor.db'))
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
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

# Upload config
app.config['UPLOAD_FOLDER'] = os.path.join(BaseDir, 'static', 'uploads', 'profiles')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 # 16 MB limit

# Initialize extensions
db.init_app(app)
mail = Mail(app)

login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Initialize Database Context
with app.app_context():
    db.create_all()

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

        user_exists = User.query.filter_by(email=email).first()
        if user_exists:
            flash('Email already in use.', 'warning')
            return redirect(url_for('register'))

        hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
        new_user = User(username=username, email=email, password=hashed_pw, role=role)
        db.session.add(new_user)
        db.session.commit()

        if role == 'tutor':
            new_profile = TutorProfile(user_id=new_user.id)
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
    return render_template('tutors.html', tutors=tutors, query=subject_query)

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
        
    subject = request.form.get('subject')
    date_str = request.form.get('date')
    time_str = request.form.get('time')
    
    try:
        booking_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        if time_str.count(':') == 2:
            booking_time = datetime.strptime(time_str, '%H:%M:%S').time()
        else:
            booking_time = datetime.strptime(time_str, '%H:%M').time()
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
    if tutor_profile and tutor_profile.user and tutor_profile.user.email:
        msg = MailMessage('New Tutoring Session Request', recipients=[tutor_profile.user.email])
        msg.body = f"Hello {tutor_profile.user.username},\n\nYou have a new tutoring session request from {current_user.username} for {subject} on {booking_date} at {booking_time}.\nPlease log in to your dashboard to confirm or decline."
        try:
            mail.send(msg)
            flash('Booking request and email sent successfully!', 'success')
        except Exception as e:
            print(f"Error sending email: {e}")
            flash('Booking requested, but failed to send email alert. Ensure your .env file has a valid Gmail App Password!', 'warning')
    else:
        flash('Booking request sent successfully!', 'success')
        
    return redirect(url_for('dashboard'))

@app.route('/update_profile', methods=['POST'])
@login_required
def update_profile():
    bio = request.form.get('bio')
    profile_photo = request.files.get('profile_photo')
    
    if current_user.role == 'tutor':
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
        filename = secure_filename(profile_photo.filename)
        # Create user specific or unique prefix
        unique_filename = f"{current_user.id}_{filename}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
        # ensure folder exists
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        profile_photo.save(filepath)
        profile.profile_photo = unique_filename
    
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
            try:
                mail.send(msg)
            except Exception as e:
                print(f"Error sending email: {e}")
    
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

if __name__ == '__main__':
    app.run(debug=True)
