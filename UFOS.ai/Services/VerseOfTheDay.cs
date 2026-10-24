using System;

namespace UFOS.ai.Services
{
    public sealed record Verse(string Reference, string Text);

    // Local, curated, public-domain (King James Version) text - deliberately no network
    // call for this card. Reliability matters more than variety here, and the set is
    // themed around diligence/wisdom/wealth to match the app's existing tone.
    public static class VerseOfTheDay
    {
        private static readonly Verse[] Verses =
        {
            new("Proverbs 10:4", "He becometh poor that dealeth with a slack hand: but the hand of the diligent maketh rich."),
            new("Proverbs 13:11", "Wealth gotten by vanity shall be diminished: but he that gathereth by labour shall increase."),
            new("Proverbs 21:5", "The thoughts of the diligent tend only to plenteousness; but of every one that is hasty only to want."),
            new("Proverbs 22:7", "The rich ruleth over the poor, and the borrower is servant to the lender."),
            new("Proverbs 27:23", "Be thou diligent to know the state of thy flocks, and look well to thy herds."),
            new("Proverbs 6:6", "Go to the ant, thou sluggard; consider her ways, and be wise."),
            new("Proverbs 14:23", "In all labour there is profit: but the talk of the lips tendeth only to penury."),
            new("Proverbs 16:3", "Commit thy works unto the LORD, and thy thoughts shall be established."),
            new("Proverbs 16:8", "Better is a little with righteousness than great revenues without right."),
            new("Proverbs 11:1", "A false balance is abomination to the LORD: but a just weight is his delight."),
            new("Proverbs 22:29", "Seest thou a man diligent in his business? he shall stand before kings."),
            new("Proverbs 24:27", "Prepare thy work without, and make it fit for thyself in the field; and afterwards build thine house."),
            new("Proverbs 27:1", "Boast not thyself of to morrow; for thou knowest not what a day may bring forth."),
            new("Proverbs 28:19", "He that tilleth his land shall have plenty of bread: but he that followeth after vain persons shall have poverty enough."),
            new("Proverbs 28:20", "A faithful man shall abound with blessings: but he that maketh haste to be rich shall not be innocent."),
            new("Proverbs 13:22", "A good man leaveth an inheritance to his children's children."),
            new("Proverbs 15:22", "Without counsel purposes are disappointed: but in the multitude of counsellors they are established."),
            new("Proverbs 19:21", "There are many devices in a man's heart; nevertheless the counsel of the LORD, that shall stand."),
            new("Proverbs 20:18", "Every purpose is established by counsel: and with good advice make war."),
            new("Proverbs 21:20", "There is treasure to be desired and oil in the dwelling of the wise; but a foolish man spendeth it up."),
            new("Proverbs 22:3", "A prudent man foreseeth the evil, and hideth himself: but the simple pass on, and are punished."),
            new("Proverbs 24:3-4", "Through wisdom is an house builded; and by understanding it is established: and by knowledge shall the chambers be filled with all precious and pleasant riches."),
            new("Proverbs 13:4", "The soul of the sluggard desireth, and hath nothing: but the soul of the diligent shall be made fat."),
            new("Proverbs 12:11", "He that tilleth his land shall be satisfied with bread: but he that followeth vain persons is void of understanding."),
            new("Proverbs 12:24", "The hand of the diligent shall bear rule: but the slothful shall be under tribute."),
            new("Proverbs 12:27", "The slothful man roasteth not that which he took in hunting: but the substance of a diligent man is precious."),
            new("Proverbs 11:28", "He that trusteth in his riches shall fall: but the righteous shall flourish as a branch."),
            new("Proverbs 15:16", "Better is little with the fear of the LORD than great treasure and trouble therewith."),
            new("Proverbs 16:16", "How much better is it to get wisdom than gold! and to get understanding rather to be chosen than silver!"),
            new("Proverbs 17:16", "Wherefore is there a price in the hand of a fool to get wisdom, seeing he hath no heart to it?"),
            new("Proverbs 19:2", "Also, that the soul be without knowledge, it is not good; and he that hasteth with his feet sinneth."),
            new("Proverbs 20:21", "An inheritance may be gotten hastily at the beginning; but the end thereof shall not be blessed."),
            new("Proverbs 22:9", "He that hath a bountiful eye shall be blessed; for he giveth of his bread to the poor."),
            new("Proverbs 23:4", "Labour not to be rich: cease from thine own wisdom."),
            new("Proverbs 27:24", "For riches are not for ever: and doth the crown endure to every generation?"),
            new("Proverbs 28:22", "He that hasteth to be rich hath an evil eye, and considereth not that poverty shall come upon him."),
            new("Ecclesiastes 11:1", "Cast thy bread upon the waters: for thou shalt find it after many days."),
            new("Ecclesiastes 9:10", "Whatsoever thy hand findeth to do, do it with thy might."),
            new("Proverbs 3:9-10", "Honour the LORD with thy substance, and with the firstfruits of all thine increase: so shall thy barns be filled with plenty."),
            new("Proverbs 20:23", "Divers weights are an abomination unto the LORD; and a false balance is not good."),
        };

        public static Verse GetForToday() => Verses[DateTime.Now.DayOfYear % Verses.Length];
    }
}
