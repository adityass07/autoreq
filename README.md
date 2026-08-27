# 🚀 Telegram Multi-User Join-DM Control Bot

Ek comprehensive Telegram Bot jisme koi bhi user Telegram ke andar aakar **apna account connect (Login) kar sakta hai** aur custom welcome message, auto-approve, aur anti-ban delays control kar sakta hai!

---

## ✨ Features

- 📱 **In-Bot Login Flow**: User direct chat me phone number + OTP (aur 2FA) daal kar apna account connect kar sakta hai. Koi session string generator ya Python coding ki zaroorat nahi.
- ✍️ **Custom DM Message Editor**: User jab chahe apna custom message type karke save kar sakta hai with tags (`{name}`, `{channel}`, `{username}`, `{full_name}`).
- ⚡ **Auto-Approve Toggle (ON / OFF)**: Join request ko turant approve karna hai ya pending chhodna hai, 1 click me toggle karein.
- ⏱️ **Anti-Ban Delay Control**: 3 human-like delay presets (Fast, Safe, Ultra-Safe) taaki account flood limit se safe rahe.
- 📊 **Live Stats & Counters**: Total DMs sent, connected phone number, aur automation state dekhne ke liye dashboard.
- 👥 **Multi-User Architecture**: Ek hi master bot par alag-alag users apni-apni IDs connect karke manage kar sakte hain.

---

## 🛠️ Step-by-Step Setup Guide

### 1. Requirements Install Karein
```bash
pip install -r requirements.txt
```

### 2. Configuration (`config.py`)
`config.py` file kholein aur apni details bharein:

1. **`BOT_TOKEN`**: [@BotFather](https://t.me/BotFather) se naya bot banayein aur uska token yahan daalein.
2. **`API_ID` & `API_HASH`**: [my.telegram.org](https://my.telegram.org) se apni API credentials daalein (Ye master credentials har user ke login ke liye use honge).

```python
API_ID = 12345678
API_HASH = "your_api_hash_here"
BOT_TOKEN = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"
```

### 3. Bot Run Karein
```bash
python run.py
```

---

## 📱 Bot ko use kaise karein (User Workflow):

1. User aapke Master Bot par `/start` bhejega.
2. **"📱 Login Account"** button par click karega.
3. Apna Phone number (e.g. `+919876543210`) bhejega.
4. Telegram par aane wala OTP enter karega (aur agar 2FA laga hai toh password daalega).
5. Login hote hi **Dashboard Menu** open ho jayega!
6. **"✍️ Edit Custom Message"** par click karke apna text likhega.
7. Ab jab bhi koi user uske channel me Join Request bhejega, uski personal profile se automated DM chala jayega! 🎉
