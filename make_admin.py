from app import app, db, User

with app.app_context():
    # Check if any users exist
    user_count = User.query.count()
    if user_count == 0:
        print("❌ No users found in the database!")
        print("📝 Please register a user first by:")
        print("   1. Starting the server: py app.py")
        print("   2. Going to http://127.0.0.1:5000/register")
        print("   3. Creating an account")
        print("   4. Then running this script again")
        exit(1)

    # Get first user (by join date)
    user = User.query.order_by(User.date_joined).first()
    if user:
        user.is_admin = True
        db.session.commit()
        print(f"✅ SUCCESS: User '{user.username}' (ID: {user.id}) is now an admin!")
        print("🔐 Login with this user to access the Admin Panel")
    else:
        print("❌ No users found")