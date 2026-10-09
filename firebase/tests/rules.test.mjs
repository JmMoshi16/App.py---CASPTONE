// Firestore Security Rules tests  (audit C-01 / H-01)
//
// Replays the REAL operations of the Flutter app, the Next.js admin console and the customers of the
// kiosk's QR codes against ../firestore.rules, as an anonymous visitor, two customers and an admin.
// Every "denied" case is an attack or a mistake the rules must stop; every "allowed" case is something the
// current apps need in order to keep working.
//
//   cd firebase/tests && npm install && npm test        (needs Java 21+; see ../README.md)
import { readFileSync } from 'node:fs';
import { after, before, beforeEach, describe, it } from 'node:test';
import { assertFails, assertSucceeds, initializeTestEnvironment } from '@firebase/rules-unit-testing';
import {
  addDoc, arrayUnion, collection, deleteDoc, doc, getCountFromServer, getDoc, getDocs, increment, limit,
  orderBy, query, runTransaction, serverTimestamp, setDoc, Timestamp, updateDoc, where, writeBatch,
} from 'firebase/firestore';

let env;

before(async () => {
  const [host, port] = (process.env.FIRESTORE_EMULATOR_HOST || '127.0.0.1:8085').split(':');
  env = await initializeTestEnvironment({
    projectId: 'demo-ecoreward',
    firestore: {
      rules: readFileSync(new URL('../firestore.rules', import.meta.url), 'utf8'),
      host,
      port: Number(port),
    },
  });
});
after(async () => { await env.cleanup(); });

const customer = (o) => ({
  role: 'customer', points: 50, createdAt: '2026-09-05T10:00:00.000', scanCount: 3, streak: 1,
  lastScanDate: '2026-10-08', badges: ['first_scan'], ...o,
});

beforeEach(async () => {
  await env.clearFirestore();
  await env.withSecurityRulesDisabled(async (ctx) => {
    const db = ctx.firestore();
    await setDoc(doc(db, 'users', 'alice'), customer({ username: 'alice', email: 'alice@example.com' }));
    await setDoc(doc(db, 'users', 'bob'), customer({ username: 'bob', email: 'bob@example.com', points: 120 }));
    await setDoc(doc(db, 'users', 'adm'), customer({ username: 'admin', email: 'admin@example.com', role: 'admin', points: 0 }));
    // alice has a profile; bob is an account from before profiles existed (no profile, no lifetimePoints)
    await setDoc(doc(db, 'profiles', 'alice'), { username: 'alice', points: 50, lifetimePoints: 50, badges: ['first_scan'] });
    const reward = (o) => ({ title: 'Free Coffee', description: 'x', pointsCost: 50, category: 'Coffee', isActive: true, ...o });
    await setDoc(doc(db, 'rewards', 'limited'), reward({ stock: 3 }));
    await setDoc(doc(db, 'rewards', 'unlimited'), reward({ stock: -1 }));
    await setDoc(doc(db, 'rewards', 'nostock'), reward({}));       // seeded rewards have no stock field
    await setDoc(doc(db, 'rewards', 'soldout'), reward({ stock: 0 }));
    const redemption = (uid) => ({ userId: uid, userEmail: `${uid}@example.com`, rewardId: 'limited', rewardTitle: 'Free Coffee',
      pointsUsed: 50, redeemedAt: '2026-10-08T10:00:00.000', status: 'pending', redemptionCode: 'ABCD1234' });
    await setDoc(doc(db, 'redemptions', 'rd_alice'), redemption('alice'));
    await setDoc(doc(db, 'redemptions', 'rd_bob'), redemption('bob'));
    const log = (uid) => ({ userId: uid, wasteType: 'Recyclable', pointsEarned: 10, timestamp: '2026-10-08T10:00:00.000', binId: 'abcd1234' });
    await setDoc(doc(db, 'waste_logs', 'l_alice'), log('alice'));
    await setDoc(doc(db, 'waste_logs', 'l_bob'), log('bob'));
    const token = (usedBy) => ({ wasteType: 'Recyclable', expiresAt: '2099-01-01T00:00:00.000', usedBy, isUsed: false,
      createdAt: '2026-10-08T10:00:00.000', claimCode: '1234' });
    await setDoc(doc(db, 'qr_tokens', 'tok1'), token([]));
    await setDoc(doc(db, 'qr_tokens', 'tok2'), { ...token(['bob']), isUsed: true });
    await setDoc(doc(db, 'qr_tokens', 'tokTs'), { ...token([]), expiresAt: Timestamp.fromDate(new Date('2099-01-01')) });
    await setDoc(doc(db, 'qr_tokens', 'tokOld'), { ...token([]), expiresAt: Timestamp.fromDate(new Date('2020-01-01')) });
    await setDoc(doc(db, 'qr_scan_history', 'h1'), { token: 'tok2', scannedByUid: 'bob', wasteType: 'Recyclable', pointsAwarded: 10 });
    await setDoc(doc(db, 'settings', 'app'), { pointsRecyclable: 10, qrExpiryMinutes: 20 });
    await setDoc(doc(db, 'bins', 'bin1'), { location: 'Cafe', fillLevel: 40 });
  });
});

const anon  = () => env.unauthenticatedContext().firestore();
const alice = () => env.authenticatedContext('alice', { email: 'alice@example.com' }).firestore();
const bob   = () => env.authenticatedContext('bob',   { email: 'bob@example.com' }).firestore();
const carol = () => env.authenticatedContext('carol', { email: 'carol@example.com' }).firestore(); // signed up, no profile yet
const admin = () => env.authenticatedContext('adm',   { email: 'admin@example.com' }).firestore();
// Phase 7 identities: custom claims are set only by tools/set_claims.py (Admin SDK).
const kiosk = () => env.authenticatedContext('kiosk1', { kiosk: true }).firestore();
const claimAdmin = () => env.authenticatedContext('staff2', { admin: true }).firestore(); // no users doc

const ok = (p) => assertSucceeds(p);
const denied = (p) => assertFails(p);

describe('anonymous visitor', () => {
  it('cannot read or write anything', async () => {
    const db = anon();
    await denied(getDoc(doc(db, 'users', 'alice')));
    await denied(getDoc(doc(db, 'rewards', 'limited')));
    await denied(getDocs(query(collection(db, 'users'), where('role', '==', 'customer'))));
    await denied(setDoc(doc(db, 'users', 'mallory'), customer({ username: 'mallory', email: 'm@example.com', points: 0, scanCount: 0, streak: 0, badges: [] })));
  });
});

describe('sign-up (Flutter FirebaseService.register)', () => {
  const fresh = (o = {}) => ({ username: 'carol', email: 'carol@example.com', role: 'customer', points: 0,
    lifetimePoints: 0, createdAt: '2026-10-09T09:00:00.000', scanCount: 0, streak: 0, badges: [], ...o });
  const freshProfile = (o = {}) => ({ username: 'carol', points: 0, lifetimePoints: 0, badges: [], ...o });
  it('allows a customer profile with zeroed stats', async () => { await ok(setDoc(doc(carol(), 'users', 'carol'), fresh())); });
  it('Phase 5 batch: users doc + matching public profile', async () => {
    const db = carol();
    await ok(writeBatch(db).set(doc(db, 'users', 'carol'), fresh()).set(doc(db, 'profiles', 'carol'), freshProfile()).commit());
  });
  it('denies a profile that does not match the user document, or starting lifetime points', async () => {
    const db = carol();
    await denied(writeBatch(db).set(doc(db, 'users', 'carol'), fresh()).set(doc(db, 'profiles', 'carol'), freshProfile({ points: 500 })).commit());
    await denied(writeBatch(db).set(doc(db, 'users', 'carol'), fresh()).set(doc(db, 'profiles', 'carol'), freshProfile({ email: 'x@y' })).commit());
    await denied(setDoc(doc(db, 'users', 'carol'), fresh({ lifetimePoints: 500 })));
  });
  it('denies signing up as admin', async () => { await denied(setDoc(doc(carol(), 'users', 'carol'), fresh({ role: 'admin' }))); });
  it('denies signing up with starting points', async () => { await denied(setDoc(doc(carol(), 'users', 'carol'), fresh({ points: 500 }))); });
  it('denies signing up with fake stats or badges', async () => {
    await denied(setDoc(doc(carol(), 'users', 'carol'), fresh({ scanCount: 50 })));
    await denied(setDoc(doc(carol(), 'users', 'carol'), fresh({ badges: ['scan_50'] })));
  });
  it('denies creating a profile for somebody else', async () => { await denied(setDoc(doc(carol(), 'users', 'dave'), fresh({ username: 'dave' }))); });
});

describe('users - reading (private since Phase 5: H-13 closed)', () => {
  it('a customer reads their own document', async () => { await ok(getDoc(doc(alice(), 'users', 'alice'))); });
  it("a customer can no longer read another customer's document (e-mail)", async () => { await denied(getDoc(doc(alice(), 'users', 'bob'))); });
  it('a customer cannot read an admin document', async () => { await denied(getDoc(doc(alice(), 'users', 'adm'))); });
  it('a customer cannot query users at all', async () => {
    await denied(getDocs(collection(alice(), 'users')));
    await denied(getDocs(query(collection(alice(), 'users'), where('role', '==', 'customer'), orderBy('points', 'desc'))));
  });
  it('an admin lists everything (web leaderboard, admin screens)', async () => {
    await ok(getDocs(collection(admin(), 'users')));
    await ok(getDocs(query(collection(admin(), 'users'), where('role', '==', 'customer'), orderBy('points', 'desc'))));
  });
});

describe('profiles - the public leaderboard', () => {
  it('any signed-in customer runs the leaderboard and rank queries', async () => {
    await ok(getDocs(query(collection(bob(), 'profiles'), orderBy('lifetimePoints', 'desc'), limit(20))));
    await ok(getCountFromServer(query(collection(bob(), 'profiles'), where('lifetimePoints', '>', 10))));
  });
  it('an anonymous visitor cannot read profiles', async () => { await denied(getDoc(doc(anon(), 'profiles', 'alice'))); });
  it("a customer cannot inflate their profile, or write someone else's", async () => {
    await denied(setDoc(doc(alice(), 'profiles', 'alice'), { username: 'alice', points: 9999, lifetimePoints: 9999, badges: ['first_scan'] }));
    await denied(setDoc(doc(alice(), 'profiles', 'bob'), { username: 'bob', points: 120, lifetimePoints: 120, badges: ['first_scan'] }));
    await denied(deleteDoc(doc(alice(), 'profiles', 'alice')));
  });
  it('username change (updateUsername): users + profile in one transaction', async () => {
    const db = alice();
    await ok(runTransaction(db, async (tx) => {
      await tx.get(doc(db, 'users', 'alice'));
      tx.update(doc(db, 'users', 'alice'), { username: 'alice2' });
      tx.set(doc(db, 'profiles', 'alice'), { username: 'alice2', points: 50, lifetimePoints: 50, badges: ['first_scan'] });
    }));
  });
});

describe('users - updating (the privilege-escalation and cheating paths)', () => {
  const a = () => doc(alice(), 'users', 'alice');
  it('H-01 payload: username only', async () => { await ok(updateDoc(a(), { username: 'alice_new' })); });
  it('a customer cannot make themselves admin', async () => { await denied(updateDoc(a(), { role: 'admin' })); });
  it('a customer cannot change their e-mail', async () => { await denied(updateDoc(a(), { email: 'ceo@example.com' })); });
  it('scan transaction (logWaste): points +15, scanCount +1, streak, lastScanDate, badges', async () => {
    const db = alice(); const ref = doc(db, 'users', 'alice');
    await ok(runTransaction(db, async (tx) => {
      const snap = await tx.get(ref);
      tx.update(ref, { points: increment(15), scanCount: increment(1), streak: 2, lastScanDate: '2026-10-09', badges: [...snap.data().badges, 'scan_5'] });
    }));
  });
  it('Phase 5 scan transaction: log with pointsAwarded + absolute user stats + profile copy', async () => {
    const db = alice();
    await ok(runTransaction(db, async (tx) => {
      await tx.get(doc(db, 'users', 'alice'));
      tx.set(doc(collection(db, 'waste_logs')), { userId: 'alice', wasteType: 'Biodegradable', pointsEarned: 15, pointsAwarded: 23,
        timestamp: Timestamp.now(), binId: 'abcd1234' });
      tx.update(doc(db, 'users', 'alice'), { points: 73, lifetimePoints: 73, scanCount: 4, streak: 3, lastScanDate: '2026-10-09',
        badges: ['first_scan', 'pts_50', 'streak_3'] });
      tx.set(doc(db, 'profiles', 'alice'), { username: 'alice', points: 73, lifetimePoints: 73, badges: ['first_scan', 'pts_50', 'streak_3'] });
    }));
  });
  it('an account from before profiles (bob) gets its profile created by its next scan', async () => {
    const db = bob();
    await ok(runTransaction(db, async (tx) => {
      await tx.get(doc(db, 'users', 'bob'));
      tx.update(doc(db, 'users', 'bob'), { points: 130, lifetimePoints: 130, scanCount: 4 });
      tx.set(doc(db, 'profiles', 'bob'), { username: 'bob', points: 130, lifetimePoints: 130, badges: ['first_scan'] });
    }));
  });
  it('lifetimePoints never decreases and grows at most +100 per write', async () => {
    await denied(updateDoc(a(), { lifetimePoints: 10 }));
    await denied(updateDoc(a(), { lifetimePoints: 5000 }));
    await denied(updateDoc(a(), { lifetimePoints: 60.5 }));
  });
  it('largest legitimate credit (+30: biodegradable 15 x 2.0 streak) is allowed', async () => { await ok(updateDoc(a(), { points: increment(30) })); });
  it('redeem: points - 50', async () => { await ok(updateDoc(a(), { points: increment(-50) })); });
  it('a customer cannot set points to a huge number in one write', async () => { await denied(updateDoc(a(), { points: 999999 })); });
  it('a customer cannot take points below zero', async () => { await denied(updateDoc(a(), { points: increment(-51) })); });
  it('a customer cannot store a non-integer balance', async () => { await denied(updateDoc(a(), { points: 50.5 })); });
  it("a customer cannot change another customer's document", async () => { await denied(updateDoc(doc(alice(), 'users', 'bob'), { points: 0 })); });
  it('an older app version that sends the whole document still works (same role/e-mail)', async () => {
    await ok(updateDoc(a(), { username: 'alice2', email: 'alice@example.com', role: 'customer', points: 50,
      createdAt: '2026-09-05T10:00:00.000000', scanCount: 3, streak: 1, lastScanDate: '2026-10-08', badges: ['first_scan'] }));
  });
  it('...but a whole-document update that smuggles role:admin is denied', async () => {
    await denied(updateDoc(a(), { username: 'alice', email: 'alice@example.com', role: 'admin', points: 50 }));
  });
  it('a customer cannot delete their document', async () => { await denied(deleteDoc(a())); });
  it('an admin can credit another customer (web Manual Claim: points increment)', async () => { await ok(updateDoc(doc(admin(), 'users', 'bob'), { points: increment(10) })); });
});

describe('rewards', () => {
  it('any signed-in user reads rewards', async () => { await ok(getDocs(collection(alice(), 'rewards'))); });
  it('customers cannot create, edit or delete rewards', async () => {
    const db = alice();
    await denied(setDoc(doc(db, 'rewards', 'free_lunch'), { title: 'Free lunch', pointsCost: 1 }));
    await denied(updateDoc(doc(db, 'rewards', 'limited'), { pointsCost: 1 }));
    await denied(updateDoc(doc(db, 'rewards', 'limited'), { title: 'Hacked' }));
    await denied(deleteDoc(doc(db, 'rewards', 'limited')));
  });
  it('redeem: limited stock 3 -> 2', async () => { await ok(updateDoc(doc(alice(), 'rewards', 'limited'), { stock: increment(-1) })); });
  it('a customer cannot lower stock by more than 1, raise it, or touch other fields with it', async () => {
    const r = () => doc(alice(), 'rewards', 'limited');
    await denied(updateDoc(r(), { stock: 0 }));
    await denied(updateDoc(r(), { stock: 999 }));
    await denied(updateDoc(r(), { stock: increment(-1), title: 'x' }));
  });
  it('unlimited (-1), seeded (no stock field) and sold-out (0) rewards cannot be "decremented"', async () => {
    await denied(updateDoc(doc(alice(), 'rewards', 'unlimited'), { stock: -2 }));
    await denied(updateDoc(doc(alice(), 'rewards', 'nostock'), { stock: -1 }));
    await denied(updateDoc(doc(alice(), 'rewards', 'soldout'), { stock: -1 }));
  });
  it('an admin creates, edits and deletes rewards', async () => {
    const db = admin();
    await ok(addDoc(collection(db, 'rewards'), { title: 'New', description: 'd', pointsCost: 10, category: 'Other', isActive: true }));
    await ok(updateDoc(doc(db, 'rewards', 'limited'), { pointsCost: 60, isActive: false }));
    await ok(deleteDoc(doc(db, 'rewards', 'nostock')));
  });
});

describe('redemptions', () => {
  const mine = (o = {}) => ({ userId: 'alice', userEmail: 'alice@example.com', rewardId: 'limited', rewardTitle: 'Free Coffee',
    pointsUsed: 50, redeemedAt: '2026-10-09T10:00:00.000', status: 'pending', redemptionCode: 'ZZZZ9999', ...o });
  it('full redeem transaction (redemption + points - cost + stock - 1)', async () => {
    const db = alice(); const rdRef = doc(collection(db, 'redemptions'));
    await ok(runTransaction(db, async (tx) => {
      await tx.get(doc(db, 'users', 'alice')); await tx.get(doc(db, 'rewards', 'limited'));
      tx.set(rdRef, mine());
      tx.update(doc(db, 'users', 'alice'), { points: increment(-50) });
      tx.update(doc(db, 'rewards', 'limited'), { stock: increment(-1) });
    }));
  });
  it('Phase 5 redeem: balance down, lifetime unchanged, profile copied', async () => {
    const db = alice(); const rdRef = doc(collection(db, 'redemptions'));
    await ok(runTransaction(db, async (tx) => {
      await tx.get(doc(db, 'users', 'alice')); await tx.get(doc(db, 'rewards', 'limited'));
      tx.set(rdRef, mine());
      tx.update(doc(db, 'users', 'alice'), { points: 0, lifetimePoints: 50 });
      tx.set(doc(db, 'profiles', 'alice'), { username: 'alice', points: 0, lifetimePoints: 50, badges: ['first_scan'] });
      tx.update(doc(db, 'rewards', 'limited'), { stock: increment(-1) });
    }));
  });
  it('a redemption cannot lower lifetimePoints', async () => {
    await denied(updateDoc(doc(alice(), 'users', 'alice'), { points: 0, lifetimePoints: 0 }));
  });
  it('a profile that keeps the old lifetime while users silently falls back to the new balance is denied', async () => {
    const db = alice();
    await denied(runTransaction(db, async (tx) => {
      await tx.get(doc(db, 'users', 'alice'));
      tx.update(doc(db, 'users', 'alice'), { points: 0 });
      tx.set(doc(db, 'profiles', 'alice'), { username: 'alice', points: 0, lifetimePoints: 50, badges: ['first_scan'] });
    }));
  });
  it('a customer cannot create a redemption for someone else, or one that is already claimed', async () => {
    await denied(addDoc(collection(alice(), 'redemptions'), mine({ userId: 'bob' })));
    await denied(addDoc(collection(alice(), 'redemptions'), mine({ status: 'claimed' })));
  });
  it('a customer reads their own redemptions (query) but not others', async () => {
    await ok(getDocs(query(collection(alice(), 'redemptions'), where('userId', '==', 'alice'))));
    await denied(getDoc(doc(alice(), 'redemptions', 'rd_bob')));
    await denied(getDocs(collection(alice(), 'redemptions')));
  });
  it('a customer cannot mark a redemption claimed or delete it', async () => {
    await denied(updateDoc(doc(alice(), 'redemptions', 'rd_alice'), { status: 'claimed' }));
    await denied(deleteDoc(doc(alice(), 'redemptions', 'rd_alice')));
  });
  it('an admin lists all and marks pending -> claimed (nothing else)', async () => {
    const db = admin();
    await ok(getDocs(collection(db, 'redemptions')));
    await ok(updateDoc(doc(db, 'redemptions', 'rd_bob'), { status: 'claimed' }));
    await denied(updateDoc(doc(db, 'redemptions', 'rd_alice'), { pointsUsed: 1 }));
    await denied(updateDoc(doc(db, 'redemptions', 'rd_alice'), { status: 'pending' }));
  });
});

describe('waste_logs', () => {
  const log = (uid) => ({ userId: uid, wasteType: 'Recyclable', pointsEarned: 10, timestamp: '2026-10-09T10:00:00.000', binId: 'abcd1234' });
  it('a customer logs their own scan; not for someone else', async () => {
    await ok(addDoc(collection(alice(), 'waste_logs'), log('alice')));
    await denied(addDoc(collection(alice(), 'waste_logs'), log('bob')));
  });
  it('pointsAwarded must be a sane integer', async () => {
    await ok(addDoc(collection(alice(), 'waste_logs'), { ...log('alice'), pointsAwarded: 30 }));
    await denied(addDoc(collection(alice(), 'waste_logs'), { ...log('alice'), pointsAwarded: 9999 }));
  });
  it('a customer reads their own logs (query) but not all, nor others', async () => {
    await ok(getDocs(query(collection(alice(), 'waste_logs'), where('userId', '==', 'alice'))));
    await denied(getDocs(collection(alice(), 'waste_logs')));
    await denied(getDoc(doc(alice(), 'waste_logs', 'l_bob')));
  });
  it('logs are append-only', async () => {
    await denied(updateDoc(doc(alice(), 'waste_logs', 'l_alice'), { pointsEarned: 9999 }));
    await denied(deleteDoc(doc(alice(), 'waste_logs', 'l_alice')));
    await denied(deleteDoc(doc(admin(), 'waste_logs', 'l_alice')));
  });
  it('an admin reads all and creates a log for a customer (Manual Claim)', async () => {
    await ok(getDocs(collection(admin(), 'waste_logs')));
    await ok(addDoc(collection(admin(), 'waste_logs'), log('bob')));
  });
});

describe('qr_tokens', () => {
  it('a customer fetches one token by id (scan) but cannot list or query tokens', async () => {
    await ok(getDoc(doc(alice(), 'qr_tokens', 'tok1')));
    await denied(getDocs(collection(alice(), 'qr_tokens')));
    await denied(getDocs(query(collection(alice(), 'qr_tokens'), where('claimCode', '==', '1234'))));
  });
  it('scan transaction as the app does it (H-02): usedBy += own uid, isUsed = true', async () => {
    const db = alice(); const ref = doc(db, 'qr_tokens', 'tok1');
    await ok(runTransaction(db, async (tx) => { await tx.get(ref); tx.update(ref, { isUsed: true, usedBy: arrayUnion('alice') }); }));
  });
  it('...also for a kiosk token whose expiresAt is a Timestamp (H-07)', async () => {
    await ok(updateDoc(doc(alice(), 'qr_tokens', 'tokTs'), { isUsed: true, usedBy: arrayUnion('alice') }));
  });
  it('an expired Timestamp token cannot be claimed (server time)', async () => {
    await denied(updateDoc(doc(alice(), 'qr_tokens', 'tokOld'), { isUsed: true, usedBy: arrayUnion('alice') }));
  });
  it('one token = one claim: a second customer cannot claim a used token', async () => {
    await denied(updateDoc(doc(alice(), 'qr_tokens', 'tok2'), { usedBy: arrayUnion('alice') }));
    await denied(updateDoc(doc(alice(), 'qr_tokens', 'tok2'), { isUsed: true, usedBy: ['alice'] }));
  });
  it('a customer cannot claim for someone else, leave isUsed false, or change anything else', async () => {
    await denied(updateDoc(doc(alice(), 'qr_tokens', 'tok1'), { isUsed: true, usedBy: arrayUnion('bob') }));
    await denied(updateDoc(doc(alice(), 'qr_tokens', 'tok1'), { usedBy: arrayUnion('alice') }));
    await denied(updateDoc(doc(alice(), 'qr_tokens', 'tok1'), { isUsed: true, usedBy: arrayUnion('alice'), expiresAt: '2099-12-31' }));
    await denied(updateDoc(doc(alice(), 'qr_tokens', 'tok1'), { wasteType: 'Biodegradable' }));
    await denied(updateDoc(doc(alice(), 'qr_tokens', 'tok1'), { isUsed: true }));
  });
  it('a customer cannot add the same uid twice, create or delete tokens', async () => {
    await denied(updateDoc(doc(bob(), 'qr_tokens', 'tok1'), { isUsed: true, usedBy: ['bob', 'bob'] }));
    await denied(setDoc(doc(alice(), 'qr_tokens', 'forged'), { wasteType: 'Biodegradable', usedBy: [], isUsed: false }));
    await denied(deleteDoc(doc(alice(), 'qr_tokens', 'tok1')));
  });
  it('an admin creates tokens (Flutter QR screen), queries by claim code and marks one used (Manual Claim)', async () => {
    const db = admin();
    await ok(setDoc(doc(db, 'qr_tokens', 'tok3'), { wasteType: 'Residual', expiresAt: '2099-01-01', usedBy: [], isUsed: false, createdAt: '2026-10-09', claimCode: '4321' }));
    await ok(getDocs(query(collection(db, 'qr_tokens'), where('claimCode', '==', '1234'), where('isUsed', '==', false))));
    await ok(runTransaction(db, async (tx) => { tx.update(doc(db, 'qr_tokens', 'tok1'), { usedBy: ['bob'], isUsed: true }); }));
  });
});

describe('qr_scan_history, settings, bins and everything else', () => {
  it('a customer writes their own scan record, never reads it, never forges another user', async () => {
    const rec = (uid) => ({ token: 'tok1', scannedByUid: uid, wasteType: 'Recyclable', pointsAwarded: 10, scannedAt: '2026-10-09T10:00:00.000' });
    await ok(addDoc(collection(alice(), 'qr_scan_history'), rec('alice')));
    await denied(addDoc(collection(alice(), 'qr_scan_history'), rec('bob')));
    await denied(getDocs(collection(alice(), 'qr_scan_history')));
    await denied(updateDoc(doc(alice(), 'qr_scan_history', 'h1'), { pointsAwarded: 999 }));
  });
  it('an admin reads the scan history', async () => { await ok(getDocs(collection(admin(), 'qr_scan_history'))); });
  it('settings: everyone signed in reads, only admin writes', async () => {
    await ok(getDoc(doc(alice(), 'settings', 'app')));
    await denied(setDoc(doc(alice(), 'settings', 'app'), { pointsRecyclable: 1000 }));
    await ok(setDoc(doc(admin(), 'settings', 'app'), { pointsRecyclable: 12, qrExpiryMinutes: 20 }));
  });
  it('bins: admin reads, nobody writes', async () => {
    await denied(getDocs(collection(alice(), 'bins')));
    await ok(getDocs(collection(admin(), 'bins')));
    await denied(setDoc(doc(admin(), 'bins', 'bin2'), { fillLevel: 1 }));
  });
  it('unknown collections are denied even for an admin (default deny)', async () => {
    await denied(getDoc(doc(admin(), 'secrets', 'x')));
    await denied(setDoc(doc(admin(), 'secrets', 'x'), { a: 1 }));
  });
});

describe('kiosk identity (Phase 7, audit C-02): least privilege', () => {
  const minutes = (m) => Timestamp.fromMillis(Date.now() + m * 60_000);
  const token = (o = {}) => ({ wasteType: 'Recyclable', expiresAt: minutes(20), usedBy: [], isUsed: false,
    createdAt: serverTimestamp(), claimCode: '4821', ...o });

  it('creates a well-formed QR token and reads it back by id (scan polling)', async () => {
    await ok(setDoc(doc(kiosk(), 'qr_tokens', 'k1'), token()));
    await ok(getDoc(doc(kiosk(), 'qr_tokens', 'k1')));
  });
  it('cannot mint a token that is already used, too long-lived, back-dated or oddly shaped', async () => {
    const db = kiosk();
    await denied(setDoc(doc(db, 'qr_tokens', 'k2'), token({ isUsed: true })));
    await denied(setDoc(doc(db, 'qr_tokens', 'k3'), token({ usedBy: ['alice'] })));
    await denied(setDoc(doc(db, 'qr_tokens', 'k4'), token({ expiresAt: minutes(24 * 60) })));
    await denied(setDoc(doc(db, 'qr_tokens', 'k5'), token({ expiresAt: minutes(-1) })));
    await denied(setDoc(doc(db, 'qr_tokens', 'k6'), token({ createdAt: Timestamp.fromMillis(0) })));
    await denied(setDoc(doc(db, 'qr_tokens', 'k7'), token({ wasteType: 'Gold' })));
    await denied(setDoc(doc(db, 'qr_tokens', 'k8'), token({ claimCode: 'ABCD' })));
    await denied(setDoc(doc(db, 'qr_tokens', 'k9'), token({ points: 5000 })));
  });
  it('cannot change, claim, list or delete tokens', async () => {
    const db = kiosk();
    await denied(updateDoc(doc(db, 'qr_tokens', 'tok1'), { isUsed: true, usedBy: arrayUnion('kiosk1') }));
    await denied(updateDoc(doc(db, 'qr_tokens', 'tok1'), { expiresAt: minutes(60) }));
    await denied(getDocs(collection(db, 'qr_tokens')));
    await denied(deleteDoc(doc(db, 'qr_tokens', 'tok1')));
  });
  it('a stolen kiosk credential cannot read any customer data', async () => {
    const db = kiosk();
    await denied(getDoc(doc(db, 'users', 'alice')));
    await denied(getDocs(collection(db, 'users')));
    await denied(getDoc(doc(db, 'profiles', 'alice')));
    await denied(getDocs(query(collection(db, 'waste_logs'), where('userId', '==', 'alice'))));
    await denied(getDocs(collection(db, 'redemptions')));
    await denied(getDocs(collection(db, 'qr_scan_history')));
    await denied(getDoc(doc(db, 'settings', 'app')));
  });
  it('cannot act as a customer or an admin', async () => {
    const db = kiosk();
    await denied(setDoc(doc(db, 'users', 'kiosk1'), { username: 'k', email: 'k@x', role: 'customer', points: 0,
      lifetimePoints: 0, scanCount: 0, streak: 0, badges: [], createdAt: '2026-10-09' }));
    await denied(addDoc(collection(db, 'waste_logs'), { userId: 'kiosk1', wasteType: 'Recyclable', pointsEarned: 10, timestamp: 'x', binId: 'b' }));
    await denied(addDoc(collection(db, 'rewards'), { title: 'Free', pointsCost: 0 }));
    await denied(updateDoc(doc(db, 'redemptions', 'rd_alice'), { status: 'claimed' }));
  });
  it('writes only its own heartbeat, with server time', async () => {
    const db = kiosk();
    await ok(setDoc(doc(db, 'kiosks', 'kiosk1'), { lastSeen: serverTimestamp(), status: 'online', name: 'CAFE-PC' }));
    await denied(setDoc(doc(db, 'kiosks', 'other-kiosk'), { lastSeen: serverTimestamp(), status: 'online' }));
    await denied(setDoc(doc(db, 'kiosks', 'kiosk1'), { lastSeen: Timestamp.fromMillis(0), status: 'online' }));
    await denied(getDoc(doc(db, 'kiosks', 'kiosk1')));
  });
  it('logs detections only in its own name', async () => {
    const det = (o = {}) => ({ wasteType: 'Recyclable', label: 'can', confidence: 0.92, modelFile: 'best.pt',
      modelSha256: 'abc', timestamp: serverTimestamp(), token: 'k1', kioskId: 'kiosk1', ...o });
    await ok(addDoc(collection(kiosk(), 'detections'), det()));
    await denied(addDoc(collection(kiosk(), 'detections'), det({ kioskId: 'other-kiosk' })));
    await denied(addDoc(collection(kiosk(), 'detections'), det({ confidence: 7 })));
    await denied(addDoc(collection(alice(), 'detections'), det()));
  });
  it('customers cannot create tokens, write heartbeats or read detections', async () => {
    await denied(setDoc(doc(alice(), 'qr_tokens', 'forged2'), token()));
    await denied(setDoc(doc(alice(), 'kiosks', 'alice'), { lastSeen: serverTimestamp(), status: 'online' }));
    await denied(getDocs(collection(alice(), 'detections')));
  });
});

describe('admin custom claim (Phase 7)', () => {
  it('an admin identified by claim (no users doc) can do admin work', async () => {
    const db = claimAdmin();
    await ok(getDocs(collection(db, 'users')));
    await ok(updateDoc(doc(db, 'redemptions', 'rd_bob'), { status: 'claimed' }));
    await ok(getDocs(collection(db, 'detections')));
  });
  it('a customer cannot fake the claim by writing it into a document', async () => {
    await denied(updateDoc(doc(alice(), 'users', 'alice'), { admin: true }));
    await denied(getDocs(collection(alice(), 'detections')));
  });
});

describe('end-to-end story: "a customer tries to become admin and take over"', () => {
  it('every step fails', async () => {
    const db = alice();
    await denied(updateDoc(doc(db, 'users', 'alice'), { role: 'admin' }));                 // 1. promote self
    await denied(setDoc(doc(db, 'users', 'alice'), customer({ username: 'alice', email: 'alice@example.com', role: 'admin' }))); // 2. overwrite profile
    await denied(setDoc(doc(db, 'rewards', 'free'), { title: 'Free', pointsCost: 0 }));    // 3. admin-only action
    await denied(updateDoc(doc(db, 'redemptions', 'rd_alice'), { status: 'claimed' }));   // 4. admin-only action
    await denied(setDoc(doc(db, 'settings', 'app'), { pointsRecyclable: 9999 }));         // 5. admin-only action
  });
});
