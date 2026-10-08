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
  addDoc, arrayUnion, collection, deleteDoc, doc, getDoc, getDocs, increment, orderBy, query,
  runTransaction, setDoc, updateDoc, where,
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
    await setDoc(doc(db, 'qr_tokens', 'tok2'), token(['bob']));
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
    createdAt: '2026-10-09T09:00:00.000', scanCount: 0, streak: 0, badges: [], ...o });
  it('allows a customer profile with zeroed stats', async () => { await ok(setDoc(doc(carol(), 'users', 'carol'), fresh())); });
  it('denies signing up as admin', async () => { await denied(setDoc(doc(carol(), 'users', 'carol'), fresh({ role: 'admin' }))); });
  it('denies signing up with starting points', async () => { await denied(setDoc(doc(carol(), 'users', 'carol'), fresh({ points: 500 }))); });
  it('denies signing up with fake stats or badges', async () => {
    await denied(setDoc(doc(carol(), 'users', 'carol'), fresh({ scanCount: 50 })));
    await denied(setDoc(doc(carol(), 'users', 'carol'), fresh({ badges: ['scan_50'] })));
  });
  it('denies creating a profile for somebody else', async () => { await denied(setDoc(doc(carol(), 'users', 'dave'), fresh({ username: 'dave' }))); });
});

describe('users - reading', () => {
  it('a customer reads their own document', async () => { await ok(getDoc(doc(alice(), 'users', 'alice'))); });
  it('a customer reads another customer (leaderboard needs this - known leak H-13)', async () => { await ok(getDoc(doc(alice(), 'users', 'bob'))); });
  it('a customer cannot read an admin document', async () => { await denied(getDoc(doc(alice(), 'users', 'adm'))); });
  it('leaderboard query: customers ordered by points', async () => {
    await ok(getDocs(query(collection(alice(), 'users'), where('role', '==', 'customer'), orderBy('points', 'desc'))));
  });
  it('rank query: customers with more points', async () => {
    await ok(getDocs(query(collection(alice(), 'users'), where('role', '==', 'customer'), where('points', '>', 10))));
  });
  it('a customer cannot list all users, or admins', async () => {
    await denied(getDocs(collection(alice(), 'users')));
    await denied(getDocs(query(collection(alice(), 'users'), where('role', '==', 'admin'))));
  });
  it('an admin lists everything', async () => { await ok(getDocs(collection(admin(), 'users'))); });
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
  it('scan transaction: appending own uid to usedBy', async () => {
    const db = alice(); const ref = doc(db, 'qr_tokens', 'tok1');
    await ok(runTransaction(db, async (tx) => { await tx.get(ref); tx.update(ref, { usedBy: arrayUnion('alice') }); }));
  });
  it('a second customer can append to a token that already has one user', async () => {
    await ok(updateDoc(doc(alice(), 'qr_tokens', 'tok2'), { usedBy: arrayUnion('alice') }));
  });
  it('a customer cannot add someone else, remove others, or change anything else', async () => {
    await denied(updateDoc(doc(alice(), 'qr_tokens', 'tok1'), { usedBy: arrayUnion('bob') }));
    await denied(updateDoc(doc(alice(), 'qr_tokens', 'tok2'), { usedBy: ['alice'] }));                 // erases bob
    await denied(updateDoc(doc(alice(), 'qr_tokens', 'tok1'), { usedBy: arrayUnion('alice'), expiresAt: '2099-12-31' }));
    await denied(updateDoc(doc(alice(), 'qr_tokens', 'tok1'), { wasteType: 'Biodegradable' }));
    await denied(updateDoc(doc(alice(), 'qr_tokens', 'tok1'), { isUsed: true }));
  });
  it('a customer cannot add the same uid twice, create or delete tokens', async () => {
    await denied(updateDoc(doc(bob(), 'qr_tokens', 'tok2'), { usedBy: ['bob', 'bob'] }));
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
